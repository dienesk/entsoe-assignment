"""Turns an ENTSO-E market document (XML) into flat, tabular rows.

Four jobs, of which flattening is only the most visible one:

1. **Reconstruct the time axis.** A ``Point`` carries no timestamp -- only a
   1-based ``position`` -- so each point's instant is
   ``period start + (position - 1) * resolution``.
2. **Fill variable-sized blocks.** With ``curveType`` ``A03`` the publisher
   sends a point only when the value *changes*; the previous value holds
   until the next position. Emitting only the published points would leave
   holes in an otherwise regular series, so gaps are carried forward and
   flagged in ``point_carried_forward`` -- an analyst can always filter back
   down to what was literally published.
3. **Denormalize the metadata.** Document- and TimeSeries-level fields are
   stated once; every row emitted under them carries its own copy.
4. **Name the columns from the document.** Nothing here hard-codes a field
   list.

Every report in the API's ``*_MarketDocument`` family nests the same way::

    <GL_MarketDocument>            -> doc_* columns (one per leaf field)
      <TimeSeries>                 -> ts_* columns (one per leaf field,
        <MktPSRType>...</MktPSRType>    including nested ones, dotted)
        <Period>
          <timeInterval>, <resolution>
          <Point><position/><quantity/></Point>   -> one row, one column
        </Period>                                    per non-position child
      </TimeSeries>
    </GL_MarketDocument>

Columns are derived from whatever leaf elements are actually present, so a
renamed dimension, an added metric, or a document type that reports
``price.amount`` where this one reports ``quantity`` changes the output
columns instead of breaking the scrape. The raw XML is stored alongside the
CSV so a materially different future shape can be reprocessed without
re-scraping.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from xml.etree import ElementTree

from dates import format_utc_instant, parse_iso8601_duration, shift_by_duration

logger = logging.getLogger(__name__)

# Constant platform identifiers (always ENTSO-E itself, on every document of
# every type). Dropped to keep the CSV readable; the document's own mRID and
# createdDateTime survive as doc_ columns, which is what actually ties a row
# back to the response it came from.
_UNINFORMATIVE_DOC_FIELDS = ("sender_MarketParticipant", "receiver_MarketParticipant")

# Guards the slot-count walk below against a pathological resolution.
_MAX_POINTS_PER_PERIOD = 100_000


class DocumentError(RuntimeError):
    """Raised when a response is not parseable as a market document."""


def _local_name(tag: str) -> str:
    """Strip the XML namespace: ``{urn:iec62325...}TimeSeries`` -> ``TimeSeries``."""
    return tag.rpartition("}")[2]


def _leaf_values(element: ElementTree.Element, exclude: frozenset[str]) -> dict[str, str]:
    """Collect every leaf descendant as ``{dotted.path: text}``.

    ``exclude`` names child tags to skip wholesale (the containers handled
    separately, e.g. ``TimeSeries`` under the document root). Repeated
    sibling tags collapse onto one column -- fine for the metadata blocks
    this is used on, where each tag appears at most once.
    """
    values: dict[str, str] = {}

    def walk(node: ElementTree.Element, path: str) -> None:
        for child in node:
            name = _local_name(child.tag)
            if name in exclude:
                continue
            child_path = f"{path}.{name}" if path else name
            if len(child):
                walk(child, child_path)
            else:
                text = (child.text or "").strip()
                if text:
                    values[child_path] = text

    walk(element, "")
    return values


def _slot_count(start: datetime, end: datetime, resolution: dict[str, int]) -> int:
    """How many whole resolution steps fit in ``[start, end)``.

    Stepped rather than divided because a P1M/P1Y resolution has no fixed
    length -- the same reason ``shift_by_duration`` does calendar arithmetic.
    """
    if not any(resolution.values()):
        raise DocumentError(f"Zero-length resolution would never advance: {resolution!r}")
    count = 0
    while count < _MAX_POINTS_PER_PERIOD and shift_by_duration(start, resolution, count + 1) <= end:
        count += 1
    return count


def _points_by_position(period: ElementTree.Element) -> dict[int, dict[str, str]]:
    """Index a period's points by position.

    A point's value columns are its children other than ``position``, named
    after the elements themselves -- ``quantity`` here, ``price.amount`` on a
    price document -- so the metric names come from the document.
    """
    points: dict[int, dict[str, str]] = {}
    for point in period.findall("./{*}Point"):
        fields = {_local_name(child.tag): (child.text or "").strip() for child in point}
        position = fields.pop("position", None)
        if position is None:
            logger.warning("Skipping a Point with no position: %r", fields)
            continue
        points[int(position)] = fields
    return points


def _period_rows(
    period: ElementTree.Element, curve_type: str | None, base_row: dict[str, Any]
) -> list[dict[str, Any]]:
    resolution = parse_iso8601_duration(period.findtext("./{*}resolution", "").strip())
    # fromisoformat accepts the "...T00:00Z" spelling the API actually uses as
    # well as the "...T00:00:00Z" and "...+00:00" variants, so a serializer
    # change upstream doesn't take the whole scrape down.
    start = datetime.fromisoformat(period.findtext("./{*}timeInterval/{*}start", "").strip())
    end = datetime.fromisoformat(period.findtext("./{*}timeInterval/{*}end", "").strip())

    points = _points_by_position(period)
    if not points:
        return []

    # The interval is authoritative for how many slots the period covers, but
    # never drop a published point that runs past it.
    last_position = max(_slot_count(start, end, resolution), max(points))
    carry_forward = curve_type == "A03"

    rows: list[dict[str, Any]] = []
    previous: dict[str, str] | None = None
    for position in range(1, last_position + 1):
        values = points.get(position)
        carried = values is None
        if carried:
            if not carry_forward or previous is None:
                continue
            values = previous
        else:
            previous = values

        rows.append(
            {
                **base_row,
                "timestamp_utc": format_utc_instant(shift_by_duration(start, resolution, position - 1)),
                "point_position": position,
                "point_carried_forward": carried,
                **values,
            }
        )
    return rows


def flatten_document(document: bytes, endpoint_name: str, area: str | None = None) -> list[dict[str, Any]]:
    """Flatten one XML market document into a list of flat row dicts.

    Each row is one (TimeSeries, period, position) triple: a timestamp plus
    the document's and series' metadata columns plus one column per metric.
    """
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise DocumentError(f"[{endpoint_name}] response is not well-formed XML: {exc}") from exc

    doc_columns = {
        f"doc_{key}": value
        for key, value in _leaf_values(root, frozenset({"TimeSeries"})).items()
        if not key.startswith(_UNINFORMATIVE_DOC_FIELDS)
    }

    rows: list[dict[str, Any]] = []
    for series in root.findall("./{*}TimeSeries"):
        base_row = {
            "endpoint_name": endpoint_name,
            "request_area": area,
            **doc_columns,
            **{f"ts_{key}": value for key, value in _leaf_values(series, frozenset({"Period"})).items()},
        }
        curve_type = series.findtext("./{*}curveType")
        for period in series.findall("./{*}Period"):
            rows.extend(_period_rows(period, curve_type, base_row))

    return rows
