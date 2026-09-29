#!/bin/bash
# Start a second Valhalla instance dedicated to Phase 5 runtime estimation.
# Port 8003 = construction (Phase 3, no traffic)
# Port 8004 = runtime    (Phase 5, WITH predicted traffic)

set -euo pipefail

VALHALLA_DATA="${VALHALLA_DATA:-$(pwd)/valhalla_runtime_data}"
mkdir -p "$VALHALLA_DATA"

# Check if the runtime Valhalla container already exists
if docker ps -a --format '{{.Names}}' | grep -q '^valhalla-runtime$'; then
    echo "Starting existing valhalla-runtime container..."
    docker start valhalla-runtime
else
    echo "Creating new valhalla-runtime container on port 8004..."

    # Copy base tiles from the construction instance if not already present
    if [ ! -d "$VALHALLA_DATA/valhalla_tiles" ]; then
        echo "Copying base tiles from construction instance..."
        docker cp valhalla-construction:/data/valhalla_tiles "$VALHALLA_DATA/"
        docker cp valhalla-construction:/data/valhalla.json "$VALHALLA_DATA/"
    fi

    docker run -d \
        --name valhalla-runtime \
        --restart unless-stopped \
        -p 8004:8002 \
        -v "$VALHALLA_DATA":/data \
        -e serve_tiles=True \
        -e build_traffic=True \
        ghcr.io/gis-ops/docker-valhalla/valhalla:latest
fi

# Wait for health
echo "Waiting for Valhalla runtime to be ready..."
for i in $(seq 1 30); do
    if curl -s http://localhost:8004/status > /dev/null 2>&1; then
        echo "Valhalla runtime ready on port 8004"
        exit 0
    fi
    sleep 2
done
echo "WARNING: Valhalla runtime did not start within 60 seconds"
exit 1
