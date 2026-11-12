
import json

# Read the original observed configuration.
with open("observed.json", encoding="utf-8") as file:
    data = json.load(file)

interfaces = data["student-host:host"]["interface"]

# Find the Wi-Fi interface.
wifi = next(
    item for item in interfaces
    if item["name"] == "Wi-Fi"
)

# Assign an MTU value that exceeds the schema limit.
wifi["mtu"] = 4294967295

# Save as a separate test file.
with open("invalid_observed.json", "w", encoding="utf-8") as file:
    json.dump(data, file, indent=2)

print("Test file created: invalid_observed.json")
