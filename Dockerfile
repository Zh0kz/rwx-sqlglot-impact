FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS source
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/source.tar.gz
COPY pack_source.py /pack_source.py
RUN mkdir /src && tar -xzf /tmp/source.tar.gz -C /src --strip-components=1 && python /pack_source.py /src /source.z

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY --from=source /source.z .
COPY --from=source /src/sqlglot /src/sqlglot
COPY --from=source /src/tests /src/tests
RUN pip install --no-cache-dir typing_extensions==4.16.0 python-dateutil==4.0.0 && python -m compileall -q /src
COPY selector.py equivalence.py replay.py map.z types.json dicts.z ./
CMD ["python", "-B", "/app/selector.py"]
