# Blast Radius

This submission selects sqlglot tests using line coverage, observed fixture opens and dialect resolution from the unmodified reference checkout. The selector reconstructs each patch against that revision, compares Python syntax trees, and unions the test sets for affected locations. Changes outside a traced function receive a broader selection. It evaluates longest-first schedules with one to eight workers using the recorded test durations.

Build with `docker build --platform linux/amd64 -t rwx-entry .`. Mount the competition data directory read-only at `/data` and run the image without arguments. The runtime uses only the Python standard library.

The upstream MIT-licensed dependency is [sqlglot](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa). The build downloads that commit. The base Python image comes from the official RWX starter. The selector and tracing code were developed with OpenAI Codex.

To regenerate the coverage artifact, use CPython 3.13 and install the competition test dependencies. From the pinned checkout, put the checkout and this repository on PYTHONPATH, set TEST_LIST to the official tests.json, MAP_OUT to an absolute destination for map.z and SKIP_INTEGRATION to 0. Run `python -m pytest -q -p build_map tests`. The tracer uses the baseline test suite only. Dataset-specific patch answers are not inputs.
