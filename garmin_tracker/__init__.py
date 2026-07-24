"""Ironman training data tracker built on top of Garmin Connect.

Modules:
    config   - environment / settings loading
    client   - authenticated Garmin Connect session (token cache + MFA)
    db       - SQLite schema and read/write helpers
    sync     - pull activities + daily wellness into the database
    metrics  - trend & progress computations (volume, CTL/ATL/TSB, etc.)
"""

__version__ = "0.1.0"
