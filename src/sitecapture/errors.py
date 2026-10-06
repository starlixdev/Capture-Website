"""Domain exceptions with stable CLI exit-code semantics."""


class SiteCaptureError(Exception):
    """Base error for expected SiteCapture failures."""

    exit_code = 1
    kind = "sitecapture_error"


class ValidationError(SiteCaptureError):
    exit_code = 2
    kind = "validation"


class PrerequisiteError(SiteCaptureError):
    exit_code = 3
    kind = "prerequisite"


class CaptureError(SiteCaptureError):
    exit_code = 4
    kind = "capture"


class ArchiveError(SiteCaptureError):
    exit_code = 5
    kind = "archive"


class PackagingError(SiteCaptureError):
    exit_code = 6
    kind = "packaging"


class CaptureCancelled(SiteCaptureError):
    exit_code = 130
    kind = "cancelled"
