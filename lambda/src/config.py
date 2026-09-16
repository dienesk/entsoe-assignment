"""Loads endpoint configuration.

In Lambda, configs live in SSM Parameter Store under ``SSM_CONFIG_PREFIX``
(one parameter per endpoint, written by Terraform from the JSON files in
``lambda/config/endpoints/``). For local development/tests, the same JSON
files are read straight off disk so no AWS access is needed to run the unit
tests. See ``lambda/config/endpoints/*.json`` for the schema, and the root
README for how to add a new endpoint.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_LOCAL_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "endpoints"


class ConfigError(RuntimeError):
    pass


def _required(config: dict[str, Any], key: str) -> Any:
    if key not in config:
        raise ConfigError(f"Endpoint config missing required field {key!r}")
    return config[key]


def validate_endpoint_config(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("endpoint_name", "method", "url", "timezone", "date_offset_days", "request_template", "s3_prefix"):
        _required(config, key)

    # "areas" is optional (not every endpoint takes an area list), but if it's
    # there it must be usable -- catching a typo here beats a confusing
    # failure once the request is already in flight.
    areas = config.get("areas")
    if areas is not None:
        if not isinstance(areas, list) or not areas:
            raise ConfigError(f"{config['endpoint_name']!r}: 'areas' must be a non-empty list, got {areas!r}")
        for area in areas:
            if not isinstance(area, str) or "|" not in area:
                raise ConfigError(
                    f"{config['endpoint_name']!r}: each area must be an '<AREA_TYPE>|<EIC>' string "
                    f"(e.g. 'CTA|10YSK-SEPS-----K'), got {area!r}"
                )
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
    response = ssm_client.get_parameter(Name=param_name)
    return validate_endpoint_config(json.loads(response["Parameter"]["Value"]))


def list_endpoint_names_from_ssm(ssm_client, prefix: str | None = None) -> list[str]:
    prefix = prefix or os.environ["SSM_CONFIG_PREFIX"]
    prefix = prefix.rstrip("/")
    names: list[str] = []
    paginator = ssm_client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=False):
        for param in page["Parameters"]:
            names.append(param["Name"].rsplit("/", 1)[-1])
    return sorted(names)
