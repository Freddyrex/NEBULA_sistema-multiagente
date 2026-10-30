#!/bin/bash

set -euo pipefail

# Print in console debug messages
echo "Starting services..."

cd nebula
echo "path $(pwd)"
# Start Gunicorn
NEBULA_SOCK=nebula.sock

echo "NEBULA_PRODUCTION: $NEBULA_PRODUCTION"
if [ "$NEBULA_PRODUCTION" = "False" ]; then
    echo "Starting Gunicorn in dev mode..."
    uvicorn nebula.controller.controller:app --host 0.0.0.0 --port $NEBULA_CONTROLLER_PORT --log-level debug --proxy-headers --forwarded-allow-ips 127.0.0.1 &
else
    echo "Starting Gunicorn in production mode..."
    uvicorn nebula.controller.controller:app --host 0.0.0.0 --port $NEBULA_CONTROLLER_PORT --log-level info --proxy-headers --forwarded-allow-ips 127.0.0.1 &
fi

tail -f /dev/null
