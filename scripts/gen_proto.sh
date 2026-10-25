#!/usr/bin/env bash
# Generate the Python gRPC code from proto/sensor_ingest.proto into app/grpc_gen/.
#   sensor_ingest_pb2.py       message classes (SubmitReadingRequest, …)
#   sensor_ingest_pb2_grpc.py  IngestServiceStub (client) and IngestServiceServicer (server base class)
# Needs grpcio-tools (requirements-dev.txt). The generated files are committed, so the
# Docker image doesn't need grpcio-tools.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
"$PY" -m grpc_tools.protoc -I proto \
  --python_out=app/grpc_gen --pyi_out=app/grpc_gen --grpc_python_out=app/grpc_gen \
  proto/sensor_ingest.proto
# protoc writes a top-level import; make it a package import.
sed -i 's/^import sensor_ingest_pb2 as/from app.grpc_gen import sensor_ingest_pb2 as/' app/grpc_gen/sensor_ingest_pb2_grpc.py
echo "generated: $(ls app/grpc_gen | tr '\n' ' ')"
