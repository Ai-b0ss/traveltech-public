"""Airport transfer adapters package.

Independent package providing normalized TransferObservation models and safe adapters
for official TfL Journey Planner, MBTA V3, and Hong Kong public datasets.
"""

from .hongkong_adapter import HongKongTransferAdapter
from .mbta_adapter import (
    LOGAN_AIRPORT_STOPS,
    SOUTH_STATION_STOP_ID,
    MBTATransferAdapter,
)
from .milan_check import check_milan_malpensa_express_api_status
from .models import (
    SnapshotDateMismatchError,
    TransferDisambiguationError,
    TransferError,
    TransferObservation,
    TransferWarning,
)
from .router import get_airport_transfer
from .sanitizer import sanitize_text
from .tfl_adapter import (
    HEATHROW_T5_RAIL_STOP_ID,
    PADDINGTON_RAIL_STOP_ID,
    TfLTransferAdapter,
    pence_to_gbp,
)
from .transport import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_CALL_BUDGET,
    MAX_RESPONSE_BYTES,
    SafeHTTPClient,
    get_live_call_count,
    strict_json_loads,
)

__all__ = [
    "TransferObservation",
    "TransferWarning",
    "TransferError",
    "TransferDisambiguationError",
    "SnapshotDateMismatchError",
    "TfLTransferAdapter",
    "MBTATransferAdapter",
    "HongKongTransferAdapter",
    "SafeHTTPClient",
    "get_airport_transfer",
    "pence_to_gbp",
    "check_milan_malpensa_express_api_status",
    "sanitize_text",
    "strict_json_loads",
    "get_live_call_count",
    "HEATHROW_T5_RAIL_STOP_ID",
    "PADDINGTON_RAIL_STOP_ID",
    "LOGAN_AIRPORT_STOPS",
    "SOUTH_STATION_STOP_ID",
    "MAX_CALL_BUDGET",
    "MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
]
