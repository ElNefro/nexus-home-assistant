# Copy to a private kontur_config.py before transferring it to the device.
# Never commit the file with your Wi-Fi password or pairing token.
WIFI_SSID = ""
WIFI_PASSWORD = ""
DEVICE_TOKEN = ""
LED_GPIO = -1  # Set this to the GPIO wired to the onboard LED on your board.
LED_ACTIVE_LOW = False  # Set True when a low GPIO level turns the LED on.

# Optional cloud control. Copy the official CA root certificate to the ESP32 as PEM or DER.
CLOUD_URL = ""  # Set to https://nexus-home-assistant-api.onrender.com to opt in.
CLOUD_TOKEN = ""  # Separate 64-character device token, never the owner's token.
CLOUD_CA_FILE = "/kontur-cloud-ca.pem"
