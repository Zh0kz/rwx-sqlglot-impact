# Blast Radius

This submission selects sqlglot tests using line coverage, observed fixture opens and dialect resolution from the unmodified reference checkout. The selector reconstructs each patch against that revision, compares Python syntax trees, and unions the test sets for affected locations. Changes outside a traced function receive a broader selection. It evaluates longest-first schedules with one to eight workers using the recorded test durations.

Build with `docker build --platform linux/amd64 -t rwx-entry .`. Mount the competition data directory read-only at `/data` and run the image without arguments. With `RUNTIME_REPLAY=1`, the selector can check small groups of original test cases against the supplied patch in an isolated process. A timeout keeps unfinished tests selected.

The upstream MIT-licensed dependency is [sqlglot](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa). The build downloads that commit. The base Python image comes from the official RWX starter. The selector and tracing code were developed with OpenAI Codex.

The type profile stores the baseline operand types seen at comparison sites. The selector can exclude an identity/equality edit when the type observations support equivalent Python semantics and the rest of the syntax tree is identical. To regenerate this file, run pytest with the `type_profile` plugin and TYPES_OUT pointing to an absolute output path. Neither the profiles nor the coverage maps take mutations or failure labels as inputs.

On Linux, run `python cold_map.py` from the sqlglot checkout with the same TEST_LIST and MAP_OUT settings to trace each unittest in a fresh child process. The module and class fixtures are run for each test. Use `python merge_maps.py map.z trace1.z trace2.z` to union compatible traces.

The `dict_profile` pytest plugin records which tests read individual keys of class dictionaries. Set DICT_OUT to the destination for dicts.z. A narrowly applicable selection uses these records when a patch changes only literal values under existing keys. Whole-dictionary reads during tests count for all keys, and dictionaries whose values were observed during import fall back to ordinary selection.

Additional baseline plugins record primitive comparison operands (`value_profile`, VALUE_OUT), condition outcomes (`bool_profile`, BOOL_OUT), expression validation inputs (`arg_profile`, ARG_OUT), and individual test-case coverage (`case_profile`, CASE_OUT). These plugins run the unmodified test suite and take the same TEST_LIST setting. The selector falls back to coverage when it cannot model an edit.

To regenerate the coverage artifact, use CPython 3.13 and install the competition test dependencies. From the pinned checkout, put the checkout and this repository on PYTHONPATH, set TEST_LIST to the official tests.json, MAP_OUT to an absolute destination for map.z and SKIP_INTEGRATION to 0. Run `python -m pytest -q -p build_map tests`. The tracer uses the baseline test suite only. Dataset-specific patch answers are not inputs.
