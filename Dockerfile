FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS source
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/source.tar.gz
COPY pack_source.py bindings.py /
RUN mkdir /src && tar -xzf /tmp/source.tar.gz -C /src --strip-components=1 && python /pack_source.py /src /source.z /references.z

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY --from=source /source.z /references.z ./
COPY --from=source /src/sqlglot /src/sqlglot
COPY --from=source /src/tests /src/tests
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 SKIP_INTEGRATION=0 RUNTIME_REPLAY=0
RUN pip install --no-cache-dir typing_extensions==4.16.0 python-dateutil==4.0.0 numpy==2.5.5 pandas==3.1.1 duckdb==2.0.0 pytz==2026.6 && python -m compileall -q /src
COPY selector.py equivalence.py behavior.py bindings.py fixtures.py replay.py probe.py warm.py cold_map.py cases.py case_hooks.py executor_cache.py map.z types.json dicts.z values.z bools.z args.z cases.z tokens.z instructions.z order.z registry.z executor.z executor_tables.z ./
CMD ["python", "-B", "/app/selector.py"]
