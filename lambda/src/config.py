"""Loads endpoint configuration and the API security token.

In Lambda, configs live in SSM Parameter Store under ``SSM_CONFIG_PREFIX``
(one parameter per endpoint, written by Terraform from the JSON files in
``lambda/config/endpoints/``). For local development/tests, the same JSON
files are read straight off disk so no AWS access is needed to run the unit
tests. See ``lambda/config/endpoints/*.json`` for the schema, and the root
README for how to add a new endpoint.

The ENTSO-E security token is deliberately *not* part of any endpoint
config: configs are plain-text SSM parameters that Terraform writes from
files in this repository. The token lives in its own SecureString parameter,
created outside Terraform so it never reaches a state file, and is read
separately by ``load_security_token``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_LOCAL_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "endpoints"

_REQUIRED_FIELDS = ("endpoint_name", "url", "timezone", "date_offset_days", "query_template", "s3_prefix")


class ConfigError(RuntimeError):
    pass


def _validate_areas(config: dict[str, Any]) -> None:
    """Check the area list, if the endpoint has one.

    Optional -- not every report is scoped to an area -- but a typo here
    surfaces as an opaque HTTP 400 from the API, so it is worth catching
    before the request is in flight.
    """
    areas = config.get("areas")
    if areas is None:
        return
    if not isinstance(areas, list) or not areas:
        raise ConfigError(f"{config['endpoint_name']!r}: 'areas' must be a non-empty list, got {areas!r}")
    for area in areas:
        if not isinstance(area, str) or not area.strip():
            raise ConfigError(f"{config['endpoint_name']!r}: each area must be a non-empty EIC string, got {area!r}")
        if "|" in area:
            # The web UI's own format, and what this scraper used before it
            # moved to the API. The API wants the bare EIC code.
            raise ConfigError(
                f"{config['endpoint_name']!r}: areas are bare EIC codes for the API "
                f"(e.g. '10YSK-SEPS-----K'), not '<AREA_TYPE>|<EIC>' -- got {area!r}"
            )


def validate_endpoint_config(config: dict[str, Any]) -> dict[str, Any]:
    for key in _REQUIRED_FIELDS:
        if key not in config:
            raise ConfigError(f"Endpoint config missing required field {key!r}")

    template = config["query_template"]
    if not isinstance(template, dict) or not template:
        raise ConfigError(f"{config['endpoint_name']!r}: 'query_template' must be a non-empty object")
    if "securityToken" in template:
        # It would end up in a plain-text SSM parameter and in this repo.
        raise ConfigError(
            f"{config['endpoint_name']!r}: 'query_template' must not contain 'securityToken' -- "
            "the token is read at runtime from its own SecureString parameter"
        )

    _validate_areas(config)
    return config


def load_local_endpoint_config(endpoint_name: str) -> dict[str, Any]:
    """Read one endpoint config JSON file from the bundled ``config/endpoints`` dir."""
    path = _LOCAL_CONFIG_DIR / f"{endpoint_name}.json"
    if not path.exists():
        raise ConfigError(f"No local config found for endpoint {endpoint_name!r} at {path}")
    return validate_endpoint_config(json.loads(path.read_text()))


def list_local_endpoint_names() -> list[str]:
    return sorted(p.stem for p in _LOCAL_CONFIG_DIR.glob("*.json"))


def load_endpoint_config_from_ssm(endpoint_name: str, ssm_client, prefix: str | None = None) -> dict[str, Any]:
    prefix = prefix or os.environ["SSM_CONFIG_PREFIX"]
    param_name = f"{prefix.rstrip('/')}/{endpoint_name}"
    try:
        response = ssm_client.get_parameter(Name=param_name)
    except ssm_client.exceptions.ParameterNotFound as exc:
        # boto3 raises this one with an empty message, so an unhandled
        # ParameterNotFound reaches CloudWatch naming neither the parameter
        # nor the endpoint. The usual cause is invoking an endpoint whose
        # config file exists locally but hasn't been applied yet -- so say
        # that, and say what is actually deployed.
        raise ConfigError(
            f"No config for endpoint {endpoint_name!r} at SSM parameter {param_name!r}. "
            f"Deployed endpoints: {_describe_available(ssm_client, prefix)}. "
            "A config reaches the Lambda only via 'terraform apply', which writes "
            "each lambda/config/endpoints/*.json file to its own SSM parameter."
        ) from exc
    return validate_endpoint_config(json.loads(response["Parameter"]["Value"]))


def _describe_available(ssm_client, prefix: str) -> str:
    """Best-effort list of deployed endpoints, for an error message only."""
    try:
        names = list_endpoint_names_from_ssm(ssm_client, prefix)
    except Exception:  # noqa: BLE001 - never let the diagnostic outshout the real error
        return "<could not be listed>"
    return ", ".join(names) or "<none>"


def list_endpoint_names_from_ssm(ssm_client, prefix: str | None = None) -> list[str]:
    prefix = prefix or os.environ["SSM_CONFIG_PREFIX"]
    prefix = prefix.rstrip("/")
    names: list[str] = []
    paginator = ssm_client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=False):
        for param in page["Parameters"]:
            names.append(param["Name"].rsplit("/", 1)[-1])
    return sorted(names)


def load_security_token(ssm_client, parameter_name: str | None = None) -> str:
    """Read the ENTSO-E API security token from its SecureString parameter.

    The parameter is created out of band (see the root README): Terraform
    grants read access to its name but never holds the value, which keeps the
    credential out of ``terraform.tfstate``. A missing parameter therefore
    means the deploy's one manual step was skipped, so say that rather than
    letting a bare ``ParameterNotFound`` surface.
    """
    parameter_name = parameter_name or os.environ["SECURITY_TOKEN_PARAMETER"]
    try:
        response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
    except ssm_client.exceptions.ParameterNotFound as exc:
        raise ConfigError(
            f"No ENTSO-E security token at SSM parameter {parameter_name!r}. Create it once with: "
            f'aws ssm put-parameter --name "{parameter_name}" --type SecureString --value "<your-token>"'
        ) from exc

    token = response["Parameter"]["Value"].strip()
    if not token:
        raise ConfigError(f"SSM parameter {parameter_name!r} is empty")
    return token
