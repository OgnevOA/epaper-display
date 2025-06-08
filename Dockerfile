# Use an official Python image for ARM64v8 (Raspberry Pi 4)
# Bullseye is the OS base for Raspberry Pi OS
FROM python:3.10-slim-bullseye

# Set the working directory inside the container
WORKDIR /app

# 1. Install system dependencies for Playwright/Chromium
# This is crucial and often missed. It installs the libraries the browser needs to run.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    # Clean up apt cache to keep image size down
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python packages
# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Install the Playwright browser binaries
# We specify --with-deps to ensure all browser dependencies are handled
RUN playwright install --with-deps chromium

# 4. Copy the rest of your application files into the container
COPY . .

# 5. Expose the ports the application uses
EXPOSE 8000
EXPOSE 8765

# 6. Command to run your application
# Replace 'your_script_name.py' with the actual name of your main python file
CMD ["python3", "bot.py"]