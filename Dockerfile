# Use the official Playwright image. The <V> tag MUST match the playwright
# pin in requirements.txt (see comment there).
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble

# Set environment variables
ENV RUNTIME_API_HOST=http://localhost \
    CHROME_INSTANCE_PATH=/usr/bin/chromium \
    HOME=/home/ubuntu

# ALLOW_SUDO=false builds a hardened image without passwordless sudo.
# NOTE: caves apply (terminal `sudo su ubuntu` usage, text_editor sudo flag);
# see README section "Hardened image".
ARG ALLOW_SUDO=true
ENV SANDBOX_ALLOW_SUDO=${ALLOW_SUDO}

# Install system dependencies and Node.js 20.x in a single layer
RUN apt-get update && \
    apt-get install -y \
        bash \
        sudo \
        curl \
        bc \
        software-properties-common && \
    # Add NodeSource and Python PPA
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    # Install all packages
    apt-get install -y \
        nodejs \
        python3.11 \
        python3.11-venv && \
    # Clean up
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Configure passwordless sudo for ubuntu user (skipped in hardened builds)
RUN if [ "${ALLOW_SUDO}" = "true" ]; then \
        echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu && \
        chmod 0440 /etc/sudoers.d/ubuntu; \
    else \
        echo "ALLOW_SUDO=false: building hardened image (no passwordless sudo)"; \
    fi

# Create the new directory structure and set ownership
RUN mkdir -p /opt/.manus/.sandbox-runtime && \
    chown -R ubuntu:ubuntu /opt/.manus

# Set working directory to the new path
WORKDIR /opt/.manus/.sandbox-runtime

# Copy application files to the new location
COPY --chown=ubuntu:ubuntu . .

# Switch to non-root user
USER ubuntu

# Create and configure Python virtual environment in the new location
RUN python3.11 -m venv venv && \
    ./venv/bin/pip install --no-cache-dir --upgrade pip && \
    ./venv/bin/pip install --no-cache-dir -r requirements.txt && \
    ./venv/bin/playwright install --with-deps

# Expose application port
EXPOSE 8330

# Start the application from the new location
CMD ["./venv/bin/python", "start_server.py"]
