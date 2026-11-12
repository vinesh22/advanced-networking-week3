
import json
import socket
import subprocess
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator


# --------------------------------------------------
# 1. Run PowerShell commands
# --------------------------------------------------

def run_powershell(command):
    """Execute a PowerShell command and return JSON output."""

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command
        ],
        capture_output=True,
        text=True,
        check=True
    )

    output = result.stdout.strip()

    if not output:
        return []

    return json.loads(output)


def as_list(value):
    """Ensure PowerShell results are always handled as lists."""

    if value is None:
        return []

    if isinstance(value, list):
        return value

    return [value]


# --------------------------------------------------
# 2. Classify network interfaces
# --------------------------------------------------

def classify_interface(name, description):

    text = (name + " " + description).lower()

    if "loopback" in text:
        return "loopback"

    if "wi-fi" in text or "wireless" in text:
        return "wifi"

    if "ethernet" in text and "virtual" not in text:
        return "ethernet"

    if "virtual" in text or "wan miniport" in text:
        return "virtual"

    return "other"


# --------------------------------------------------
# 3. Collect network configuration
# --------------------------------------------------

def collect_configuration():

    adapters = as_list(run_powershell(
        "Get-NetAdapter -IncludeHidden | "
        "Select-Object Name, InterfaceDescription, ifIndex, Status, MacAddress "
        "| ConvertTo-Json -Depth 5"
    ))

    ip_interfaces = as_list(run_powershell(
        "Get-NetIPInterface | "
        "Select-Object InterfaceAlias, AddressFamily, ConnectionState, "
        "NlMtu | ConvertTo-Json -Depth 5"
    ))

    addresses = as_list(run_powershell(
        "Get-NetIPAddress | "
        "Select-Object InterfaceAlias, AddressFamily, IPAddress, PrefixLength "
        "| ConvertTo-Json -Depth 5"
    ))

    dns_records = as_list(run_powershell(
        "Get-DnsClientServerAddress | "
        "Select-Object InterfaceAlias, ServerAddresses "
        "| ConvertTo-Json -Depth 5"
    ))

    routes = as_list(run_powershell(
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
        "| Where-Object {$_.NextHop -ne '0.0.0.0'} "
        "| Sort-Object RouteMetric, InterfaceMetric "
        "| Select-Object -First 1 NextHop "
        "| ConvertTo-Json -Depth 5"
    ))

    gateway = routes[0].get("NextHop") if routes else None

    # Build MTU lookup by interface name.
    mtu_map = {}

    for item in ip_interfaces:

        alias = item.get("InterfaceAlias")
        family = str(item.get("AddressFamily", "")).lower()
        mtu = item.get("NlMtu")

        if alias and mtu is not None:

            if family == "ipv4" or alias not in mtu_map:
                mtu_map[alias] = mtu

    # Group IP addresses by interface.
    ip_map = {}

    for item in addresses:

        alias = item.get("InterfaceAlias")
        family = str(item.get("AddressFamily", "")).lower()
        address = item.get("IPAddress")
        prefix = item.get("PrefixLength")

        if not alias or not address:
            continue

        if alias not in ip_map:

            ip_map[alias] = {
                "ipv4": [],
                "ipv6": []
            }

        if family in ("ipv4", "ipv6"):

            key = "ipv4" if family == "ipv4" else "ipv6"
            entry = f"{address}/{prefix}"

            ip_map[alias][key].append(entry)

    # Collect DNS servers for the active Wi-Fi adapter.
    dns_servers = []

    for item in dns_records:

        if item.get("InterfaceAlias") == "Wi-Fi":

            for server in as_list(item.get("ServerAddresses")):

                if server and server not in dns_servers:
                    dns_servers.append(server)

    # Build interface records.
    interfaces = []

    for adapter in adapters:

        name = adapter.get("Name", "")
        description = adapter.get("InterfaceDescription", "")
        status = str(adapter.get("Status", "")).lower()

        # Exclude adapters marked as not present.
        if status == "not present":
            continue

        oper_status = "up" if status == "up" else "down"

        mac = adapter.get("MacAddress")

        if mac:
            mac = mac.replace("-", ":").lower()

        interface = {
            "name": name,
            "type": classify_interface(name, description),
            "oper-status": oper_status
        }

        if mac:
            interface["mac"] = mac

        # Include MTU only when it satisfies the schema.
        mtu = mtu_map.get(name)

        if mtu is not None:

            mtu = int(mtu)

            if 68 <= mtu <= 65536:
                interface["mtu"] = mtu

        # Add IPv4 and IPv6 addresses.
        interface_ips = ip_map.get(
            name,
            {"ipv4": [], "ipv6": []}
        )

        if interface_ips["ipv4"]:
            interface["ipv4"] = interface_ips["ipv4"]

        if interface_ips["ipv6"]:
            interface["ipv6"] = interface_ips["ipv6"]

        interfaces.append(interface)

    # Build host configuration.
    host = {
        "hostname": socket.gethostname(),
        "os": "windows",
        "collected-at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "interface": interfaces
    }

    if gateway:
        host["default-gateway"] = gateway

    if dns_servers:
        host["dns-server"] = dns_servers

    return {
        "student-host:host": host
    }


# --------------------------------------------------
# 4. Validate JSON against the supplied schema
# --------------------------------------------------

def validate_configuration(configuration, schema_file):

    with open(schema_file, encoding="utf-8") as file:
        schema = json.load(file)

    validator = Draft202012Validator(schema)

    errors = sorted(
        validator.iter_errors(configuration),
        key=lambda error: list(map(str, error.absolute_path))
    )

    if not errors:

        print("\nSchema validation: VALID")
        return True

    print("\nSchema validation: INVALID")

    for error in errors:

        path = "/".join(
            map(str, error.absolute_path)
        ) or "<root>"

        print(f"Path: {path} | {error.message}")

    return False


# --------------------------------------------------
# 5. Compare intended and observed configurations
# --------------------------------------------------

def compare_expected(expected, actual, path=""):

    differences = []

    if isinstance(expected, dict):

        if not isinstance(actual, dict):
            return [f"{path}: expected an object"]

        for key, expected_value in expected.items():

            current_path = f"{path}/{key}"

            if key not in actual:

                differences.append(
                    f"{current_path}: missing from observed configuration"
                )

            else:

                differences.extend(
                    compare_expected(
                        expected_value,
                        actual[key],
                        current_path
                    )
                )

    elif isinstance(expected, list):

        if not isinstance(actual, list):
            return [f"{path}: expected a list"]

        # Match interface objects by their names.
        if all(
            isinstance(item, dict) and "name" in item
            for item in expected
        ):

            actual_by_name = {
                item["name"]: item
                for item in actual
                if isinstance(item, dict) and "name" in item
            }

            for item in expected:

                name = item["name"]

                if name not in actual_by_name:

                    differences.append(
                        f"{path}/{name}: interface missing"
                    )

                else:

                    differences.extend(
                        compare_expected(
                            item,
                            actual_by_name[name],
                            f"{path}/{name}"
                        )
                    )

        else:

            # Treat intended list entries as required values.
            for item in expected:

                if item not in actual:

                    differences.append(
                        f"{path}: expected value {item!r} not found"
                    )

    elif expected != actual:

        differences.append(
            f"{path}: expected {expected!r}, observed {actual!r}"
        )

    return differences


def detect_drift(observed, intended):

    expected_host = intended["student-host:host"]
    actual_host = observed["student-host:host"]

    differences = compare_expected(
        expected_host,
        actual_host,
        "student-host:host"
    )

    print("\nConfiguration drift:")

    if differences:

        for difference in differences:
            print("-", difference)

    else:

        print("No drift detected for the intended fields.")


# --------------------------------------------------
# 6. Main program
# --------------------------------------------------

def main():

    folder = Path(__file__).parent

    schema_file = folder / "host_schema.json"
    intended_file = folder / "intended.json"
    output_file = folder / "observed.json"

    # Collect actual network configuration.
    configuration = collect_configuration()

    # Save observed configuration.
    with output_file.open("w", encoding="utf-8") as file:

        json.dump(
            configuration,
            file,
            indent=2
        )

    print("Network configuration collected successfully.")

    print(f"Output saved to: {output_file}")

    print(
        "Interfaces collected:",
        len(configuration["student-host:host"]["interface"])
    )

    # Validate collected configuration.
    validate_configuration(
        configuration,
        schema_file
    )

    # Read intended configuration.
    with intended_file.open(encoding="utf-8") as file:
        intended = json.load(file)

    # Detect configuration drift.
    detect_drift(
        configuration,
        intended
    )


if __name__ == "__main__":
    main()
