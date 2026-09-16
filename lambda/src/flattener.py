"""Generic flattener for ENTSO-E Transparency Platform "business report" responses.

Every endpoint on the platform that follows the uuApp report convention
(day-ahead forecast, actual generation per unit, and — per the task — any
future endpoint of the same family) returns the same envelope shape:

    {
      "instanceList": [
        {
          "businessDimensionMap": {...},        # -> one column per key
          "instanceAttributeMap": {...},         # -> one column per key (optional)
          "curveData": {
            "pointAttributeVariabilityMap": {...},  # ordered list of metric names
            "periodList": [
              {"timeInterval": {"from": ...}, "resolution": "PT60M",
               "pointMap": {"0": [...], "1": [...], ...}}
            ]
          }
        }
      ]
    }

Rather than hard-coding column names, this module derives them from whatever
keys are actually present, so a schema change upstream (a renamed dimension,
an added metric, an extra period) changes the output columns automatically
instead of breaking the scrape.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from dates import format_utc_instant, parse_iso8601_duration_minutes, parse_utc_instant

# Order in which a point's value is looked for. "value" is the conventional
# key for a populated data point; "alt" is what the platform sends instead
# when there's no data yet (e.g. "N/A", "n/e"). Anything else falls back to
# a raw JSON dump of the point so an unexpected shape never crashes a run.
_VALUE_KEYS = ("value", "alt")


def _extract_point_value(point: Any) -> Any:
    if not isinstance(point, dict):
        return point
    for key in _VALUE_KEYS:
        if key in point and point[key] is not None:
            return point[key]
    if not point:
        return None
    import json

    return json.dumps(point, sort_keys=True)


def flatten_response(payload: dict[str, Any], endpoint_name: str) -> list[dict[str, Any]]:
    """Flatten one API response into a list of flat row dicts.

    Each row is one (instance, period, point-index) triple: a timestamp plus
    the instance's dimension/attribute columns plus one column per metric.
    """
    rows: list[dict[str, Any]] = []
    data_view_code = payload.get("dataViewCode")

    for instance in payload.get("instanceList", []) or []:
        dim_columns = {
            f"dim_{key}": value
            for key, value in (instance.get("businessDimensionMap") or {}).items()
        }
        attr_columns = {
            f"attr_{key}": value
            for key, value in (instance.get("instanceAttributeMap") or {}).items()
        }

        curve_data = instance.get("curveData") or {}
        # dict preserves insertion order (Python 3.7+); this order is what
        # lines up positionally with each point's value array below.
        metric_names = list((curve_data.get("pointAttributeVariabilityMap") or {}).keys())

        for period in curve_data.get("periodList", []) or []:
            resolution_minutes = parse_iso8601_duration_minutes(period["resolution"])
            period_start = parse_utc_instant(period["timeInterval"]["from"])
            point_map: dict[str, list[Any]] = period.get("pointMap") or {}

            for index_str, point_values in point_map.items():
                timestamp = period_start + timedelta(minutes=resolution_minutes * int(index_str))
                row = {
                    "endpoint_name": endpoint_name,
                    "data_view_code": data_view_code,
                    "timestamp_utc": format_utc_instant(timestamp),
                    **dim_columns,
                    **attr_columns,
                }
                for position, metric_name in enumerate(metric_names):
                    value = point_values[position] if position < len(point_values) else None
                    row[metric_name] = _extract_point_value(value)
                rows.append(row)

    return rows
