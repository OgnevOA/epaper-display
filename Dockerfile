# Step 1: Use the official, pre-configured Playwright image.
# It's multi-arch, so it works on your Orange Pi (ARM64) and your PC (AMD64).
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Step 2: Set the working directory
WORKDIR /

# Step 3: Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 4: Copy your application files (script, fonts, json)
# We copy everything, but volumes will override settings.json and friends.json at runtime.
COPY . .

# Step 5: Expose the ports
EXPOSE 8000
EXPOSE 8765

# Step 6: Define the command to run your script
# Replace 'your_script.py' with the actual name of your file
CMD ["python3", "bot.py"]