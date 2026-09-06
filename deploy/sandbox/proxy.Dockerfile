FROM docker:27.5.1-cli AS docker-cli

FROM python:3.11.13-alpine3.22
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /opt/meridian-proxy
COPY deploy/sandbox/requirements-proxy.txt ./requirements-proxy.txt
RUN pip install --no-cache-dir --require-hashes -r requirements-proxy.txt
COPY backend/services/data_plane/sandbox.py ./sandbox.py
COPY deploy/sandbox/host_proxy.py ./host_proxy.py
EXPOSE 8090
ENTRYPOINT ["python3", "host_proxy.py"]
