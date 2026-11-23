"""Skip POSIX-only frozen research spike tests on Windows."""

import sys

if sys.platform == "win32":
    collect_ignore = [
        "m2/argv/test_spike_prepare_work.py",
        "m2/scale/proxy/tests/test_scale_proxy_drift.py",
        "readme/tests/test_readme_assets.py",
    ]
