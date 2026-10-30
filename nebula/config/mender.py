import base64
import json
import logging
import os
import subprocess

import requests


class MenderError(RuntimeError):
    """Raised when a Mender operation cannot be completed."""

    def __init__(self, message: str):
        super().__init__(message)


class MenderRequestError(MenderError):
    """Raised when the Mender API rejects a request.

    Args:
        action (str): Description of the failed operation.
        status_code (int): HTTP status returned by the server.
    """

    def __init__(self, action: str, status_code: int):
        message = f"Mender {action} failed with HTTP status {status_code}"
        super().__init__(message)


class Mender:
    def __init__(self):
        self.server = os.environ.get("MENDER_SERVER")
        self.user = os.environ.get("MENDER_USER")
        self.password = os.environ.get("MENDER_PASSWORD")
        self.token = os.environ.get("MENDER_TOKEN")
        logging.info(f"Mender server: {self.server}")

    def get_token(self) -> str:
        """Return the current Mender API token."""
        return self.token

    def renew_token(self) -> None:
        """Renew the Mender API token using the configured credentials.

        The token is parsed from the ``token`` field of the login response and
        never logged.

        Raises:
            RuntimeError: If the credentials are missing, the server rejects
                the login or the response carries no token.
        """
        if not self.user or not self.password:
            raise MenderError(message="Mender credentials are not configured")

        string = self.user + ":" + self.password
        base64string = base64.b64encode(string.encode("utf-8"))
        headers = {
            "Accept": "application/json",
            "Authorization": "Basic {}".format(str(base64string, "utf-8")),
            "Content-Type": "application/json",
        }

        r = requests.post(f"{self.server}/api/management/v1/useradm/auth/login", headers=headers)
        if not r.ok:
            raise MenderRequestError(action="login", status_code=r.status_code)

        try:
            payload = r.json()
        except ValueError as exc:
            raise MenderError(message="Mender login response was not valid JSON") from exc

        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise MenderError(message="Mender login response did not contain a token")

        self.token = str(token)
        logging.info(f"Mender token renewed (status {r.status_code})")

    @staticmethod
    def build_artifact_command(
        type_artifact: str,
        artifact_name: str,
        device_type: str,
        file_path: str,
    ) -> list[str]:
        """Build the ``mender-artifact`` argv list.

        The command is returned as an argument vector so that caller supplied
        values can never be interpreted by a shell.

        Args:
            type_artifact (str): Artifact type (``rootfs`` or ``module``).
            artifact_name (str): Name of the artifact to write.
            device_type (str): Device type the artifact targets.
            file_path (str): Path of the image to package.

        Returns:
            list[str]: The argument vector for the ``mender-artifact`` binary.
        """
        return [
            "mender-artifact",
            "write",
            "module-image",
            "-T",
            str(type_artifact),
            "-n",
            str(artifact_name),
            "-t",
            str(device_type),
            "-o",
            f"{artifact_name}.mender",
            "-f",
            str(file_path),
        ]

    @staticmethod
    def generate_artifact(
        type_artifact: str,
        artifact_name: str,
        device_type: str,
        file_path: str,
    ) -> None:
        """Write a Mender module image artifact through the ``mender-artifact`` CLI.

        Args:
            type_artifact (str): Artifact type (``rootfs`` or ``module``).
            artifact_name (str): Name of the artifact to write.
            device_type (str): Device type the artifact targets.
            file_path (str): Path of the image to package.

        Raises:
            RuntimeError: If the binary is missing or the command fails.
        """
        command = Mender.build_artifact_command(type_artifact, artifact_name, device_type, file_path)
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise MenderError(message="mender-artifact executable was not found") from exc
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip()[:500]
            message = f"mender-artifact failed with exit code {exc.returncode}: {details}"
            raise MenderError(message=message) from exc

        logging.info(f"Mender artifact written: {artifact_name}.mender")

    def get_artifacts(self) -> list[str]:
        """List the artefact names available in the Mender server.

        Returns:
            list[str]: The artefact names reported by the server.

        Raises:
            RuntimeError: If the server rejects the request.
        """
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        r = requests.get(f"{self.server}/api/management/v1/deployments/artifacts", headers=headers)
        if not r.ok:
            raise MenderRequestError(action="artifacts request", status_code=r.status_code)

        try:
            payload = r.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            payload = payload.get("artifacts")
        if not isinstance(payload, list):
            payload = []

        names = [str(item.get("name")) for item in payload if isinstance(item, dict) and item.get("name")]
        logging.info(f"Mender artifacts listed (status {r.status_code}): {', '.join(names) if names else 'none'}")
        return names

    def upload_artifact(self, artifact_path, description):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        multipart_form_data = {
            "description": (None, f"{description}"),
            "artifact": (None, open(f"{artifact_path}", "rb")),
        }

        r = requests.post(
            f"{self.server}/api/management/v1/deployments/artifacts",
            files=multipart_form_data,
            headers=headers,
        )
        if not r.ok:
            raise MenderRequestError(action="artifact upload", status_code=r.status_code)

        logging.info(f"Mender artifact uploaded (status {r.status_code})")

    def deploy_artifact_device(self, artifact_name, device):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }

        json_req = {
            "artifact_name": f"{artifact_name}",
            "devices": [f"{device}"],
            "name": f"{device}",
            "all_devices": False,
        }

        r = requests.post(
            f"{self.server}/api/management/v1/deployments/deployments",
            json=json_req,
            headers=headers,
        )
        if not r.ok:
            raise MenderRequestError(action="deployment", status_code=r.status_code)

        logging.info(f"Mender deployment requested (status {r.status_code}, artifact {artifact_name})")

    def deploy_artifact_list(self, artifact_name, devices: list[str]):
        headers = {
            "Accept": "application/json",
            "Authorization": "Basic {}".format((self.user + ":" + self.password).encode("base64")),
            "Content-Type": "application/form-data",
        }

        json_req = {
            "artifact_name": f"{artifact_name}",
            "devices": devices,
            "name": "Example_deployment",
        }

        r = requests.post(
            f"{self.server}/api/management/v1/deployments/deployments",
            json=json_req,
            headers=headers,
        )
        if not r.ok:
            raise MenderRequestError(action="deployment", status_code=r.status_code)

        logging.info(f"Mender deployment requested (status {r.status_code}, artifact {artifact_name})")

    def get_info_deployment(self, deployment_id):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        r = requests.get(
            f"{self.server}/api/management/v1/deployments/deployments/{deployment_id}",
            headers=headers,
        )
        if not r.ok:
            raise MenderRequestError(action="deployment request", status_code=r.status_code)

        logging.info(f"Mender deployment info retrieved (status {r.status_code})")

    def get_my_info(self):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        r = requests.get(f"{self.server}/api/management/v1/useradm/users/me", headers=headers)
        if not r.ok:
            raise MenderRequestError(action="user request", status_code=r.status_code)

        logging.info(f"Mender user info retrieved (status {r.status_code})")

    def get_devices(self):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        r = requests.get(
            f"{self.server}/api/management/v2/devauth/devices",
            params={"per_page": "300"},
            headers=headers,
        )
        if not r.ok:
            raise MenderRequestError(action="devices request", status_code=r.status_code)

        logging.info(f"Mender devices retrieved (status {r.status_code})")

    def get_devices_by_group(self, group):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        json_req = {
            "page": 1,
            "per_page": 300,
            "filters": [
                {
                    "scope": "system",
                    "attribute": "group",
                    "type": "$eq",
                    "value": group,
                },
                {
                    "scope": "identity",
                    "attribute": "status",
                    "type": "$eq",
                    "value": "accepted",
                },
            ],
            "sort": [],
            "attributes": [
                {"scope": "identity", "attribute": "status"},
                {"scope": "inventory", "attribute": "artifact_name"},
                {"scope": "inventory", "attribute": "device_type"},
                {"scope": "inventory", "attribute": "rootfs-image.version"},
                {"scope": "monitor", "attribute": "alerts"},
                {"scope": "system", "attribute": "created_ts"},
                {"scope": "system", "attribute": "updated_ts"},
                {"scope": "tags", "attribute": "name"},
                {"scope": "identity", "attribute": "name"},
            ],
        }

        r = requests.post(
            f"{self.server}/api/management/v2/inventory/filters/search",
            json=json_req,
            headers=headers,
        )
        if not r.ok:
            raise MenderRequestError(action="inventory search", status_code=r.status_code)

        logging.info(f"Mender inventory searched (status {r.status_code})")

        # json to file
        with open(f"devices_{group}.json", "w") as outfile:
            json.dump(r.json(), outfile, indent=2)

    def get_info_device(self, device_id):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        r = requests.get(
            f"{self.server}/api/management/v1/inventory/devices/{device_id}",
            headers=headers,
        )
        if not r.ok:
            raise MenderRequestError(action="device request", status_code=r.status_code)

        logging.info(f"Mender device info retrieved (status {r.status_code})")

    def get_connected_device(self, device_id):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        r = requests.get(
            f"{self.server}/api/management/v1/deviceconnect/devices/{device_id}",
            headers=headers,
        )
        if not r.ok:
            raise MenderRequestError(action="connected device request", status_code=r.status_code)

        logging.info(f"Mender connected device info retrieved (status {r.status_code})")
