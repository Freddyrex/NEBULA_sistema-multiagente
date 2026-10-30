import logging
import os
import socket

import docker


class FileUtils:
    """
    Utility class for file operations.
    """

    @classmethod
    def check_path(cls, base_path: str, relative_path: str) -> str:
        """
        Joins and normalizes a base path with a relative path, then validates
        that the resulting full path is inside the base path directory.

        The containment check compares resolved path components, so sibling
        directories sharing a textual prefix (e.g. ``/app/logs_secret`` against
        ``/app/logs``) are rejected.

        Args:
            base_path (str): The base directory path.
            relative_path (str): The relative path to join with the base path.

        Returns:
            str: The normalized full path.

        Raises:
            Exception: If the resulting path is outside the base directory.
        """
        full_path = os.path.normpath(os.path.join(base_path, relative_path))
        real_base = os.path.realpath(base_path)
        real_full = os.path.realpath(full_path)

        try:
            common_path = os.path.commonpath([real_full, real_base])
        except ValueError:
            common_path = ""

        if common_path != real_base:
            raise Exception("Not allowed")
        return full_path


class EnvUtils:
    """
    Utility class for reading and updating ``.env`` files.
    """

    @staticmethod
    def read_env_file(env_path):
        """
        Reads a ``.env`` file and returns its key/value pairs.

        Args:
            env_path (str): Path to the ``.env`` file.

        Returns:
            dict: Mapping of key to value. Empty dict if the file is missing or unreadable.
        """
        values = {}
        try:
            with open(env_path) as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            return {}
        return values

    @staticmethod
    def persist_credentials(env_path, user, password, extra=None):
        """
        Writes the admin credentials into a ``.env`` file without duplicating keys.

        Existing ``NEBULA_DEFAULT_USER`` / ``NEBULA_DEFAULT_PASSWORD`` lines are
        replaced; missing ones are appended. Extra keys can be written at the
        same time (for example ``NEBULA_ADMIN_PASSWORD_PENDING``).

        Args:
            env_path (str): Path to the ``.env`` file.
            user (str): Admin username.
            password (str): Admin password.
            extra (dict | None): Additional key/value pairs to write.

        Returns:
            bool: True if the file was written, False on failure.
        """
        wanted = {"NEBULA_DEFAULT_USER": user, "NEBULA_DEFAULT_PASSWORD": password}
        if extra:
            wanted.update(extra)
        try:
            lines = []
            if os.path.isfile(env_path):
                with open(env_path) as f:
                    lines = f.read().splitlines()

            remaining = dict(wanted)
            new_lines = []
            for line in lines:
                key = line.split("=", 1)[0].strip()
                if key in remaining:
                    new_lines.append(f"{key}={remaining.pop(key)}")
                else:
                    new_lines.append(line)
            for key, value in remaining.items():
                new_lines.append(f"{key}={value}")

            with open(env_path, "w") as f:
                f.write("\n".join(new_lines).rstrip("\n") + "\n")
        except OSError as e:
            logging.warning(f"No se pudieron persistir las credenciales en {env_path}: {e!s}")
            return False
        else:
            return True


class SocketUtils:
    """
    Utility class for socket operations.
    """

    @classmethod
    def is_port_open(cls, port):
        """
        Checks if a TCP port is available (not currently bound) on localhost.

        Args:
            port (int): The port number to check.

        Returns:
            bool: True if the port is free (available), False if it is in use.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR avoids false "busy" readings from lingering TIME_WAIT
        # connections (e.g. after a container holding the port was removed).
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return True
        except OSError:
            return False

    @classmethod
    def find_free_port(cls, start_port=49152, end_port=65535):
        """
        Finds and returns the first available TCP port within the specified range.

        Args:
            start_port (int, optional): Starting port number to check. Defaults to 49152.
            end_port (int, optional): Ending port number to check. Defaults to 65535.

        Returns:
            int or None: The first free port found, or None if no free port is found.
        """
        for port in range(start_port, end_port + 1):
            if cls.is_port_open(port):
                return port
        return None


class DockerUtils:
    """
    Utility class for Docker operations such as creating networks,
    checking containers, and removing networks or containers by name prefix.
    """

    @classmethod
    def create_docker_network(cls, network_name, subnet=None, prefix=24):
        """
        Creates a Docker bridge network with a given name and optional subnet.
        If subnet is None or already in use, it finds an available subnet in
        the 192.168.X.0/24 range starting from 192.168.50.0/24.

        Args:
            network_name (str): Name of the Docker network to create.
            subnet (str, optional): Subnet in CIDR notation (e.g. "192.168.50.0/24").
            prefix (int, optional): Network prefix length, default is 24.

        Returns:
            str or None: The base subnet (e.g. "192.168.50") of the created or existing
                         network, or None if an error occurred.
        """
        try:
            # Connect to Docker
            import os

            client = docker.from_env()
            # §2.3/§3.4: base de subred por env (default legacy 192.168).
            base_subnet = os.environ.get("NEBULA_DOCKER_SUBNET_BASE", "192.168")

            # Obtain existing docker subnets
            existing_subnets = []
            networks = client.networks.list()

            existing_network = next((n for n in networks if n.name == network_name), None)

            if existing_network:
                ipam_config = existing_network.attrs.get("IPAM", {}).get("Config", [])
                if ipam_config:
                    # Assume there's only one subnet per network for simplicity
                    existing_subnet = ipam_config[0].get("Subnet", "")
                    potential_base = ".".join(existing_subnet.split(".")[:3])  # Extract base from subnet
                    logging.info(f"Network '{network_name}' already exists with base {potential_base}")
                    return potential_base

            for network in networks:
                ipam_config = network.attrs.get("IPAM", {}).get("Config", [])
                if ipam_config:
                    for config in ipam_config:
                        if "Subnet" in config:
                            existing_subnets.append(config["Subnet"])

            # If no subnet is provided or it exists, find the next available one
            if not subnet or subnet in existing_subnets:
                import os as _os

                _start = int(_os.environ.get("NEBULA_DOCKER_SUBNET_START", 50))
                for i in range(_start, 255):  # Iterate over 192.168.{start}.0 to 192.168.254.0
                    subnet = f"{base_subnet}.{i}.0/{prefix}"
                    potential_base = f"{base_subnet}.{i}"
                    if subnet not in existing_subnets:
                        break
                else:
                    raise ValueError("No available subnets found.")

            # Create the Docker network
            gateway = f"{subnet.split('/')[0].rsplit('.', 1)[0]}.1"
            ipam_pool = docker.types.IPAMPool(subnet=subnet, gateway=gateway)
            ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
            network = client.networks.create(name=network_name, driver="bridge", ipam=ipam_config)

            logging.info(f"Network created: {network.name} with subnet {subnet}")
            return potential_base

        except docker.errors.APIError:
            logging.exception("Error interacting with Docker")
            return None
        except Exception:
            logging.exception("Unexpected error")
            return None
        finally:
            client.close()  # Ensure the Docker client is closed

    @classmethod
    def check_docker_by_prefix(cls, prefix):
        """
        Checks if there are any Docker containers whose names start with the given prefix.

        Args:
            prefix (str): Prefix string to match container names.

        Returns:
            bool: True if any container matches the prefix, False otherwise.
        """
        try:
            # Connect to Docker client
            client = docker.from_env()

            containers = client.containers.list(all=True)  # `all=True` to include stopped containers

            # Iterate through containers and remove those with the matching prefix
            for container in containers:
                if container.name.startswith(prefix):
                    return True

            return False

        except docker.errors.APIError:
            logging.exception("Error interacting with Docker")
        except Exception:
            logging.exception("Unexpected error")

    @classmethod
    def remove_docker_network(cls, network_name):
        """
        Removes a Docker network by name.

        Args:
            network_name (str): Name of the Docker network to remove.

        Returns:
            None
        """
        try:
            # Connect to Docker
            client = docker.from_env()

            # Get the network by name
            network = client.networks.get(network_name)

            # Remove the network
            network.remove()

            logging.info(f"Network {network_name} removed successfully.")
        except docker.errors.NotFound:
            logging.exception(f"Network {network_name} not found.")
        except docker.errors.APIError:
            logging.exception("Error interacting with Docker")
        except Exception:
            logging.exception("Unexpected error")

    @classmethod
    def remove_docker_networks_by_prefix(cls, prefix):
        """
        Removes all Docker networks whose names start with the given prefix.

        Args:
            prefix (str): Prefix string to match network names.

        Returns:
            None
        """
        try:
            # Connect to Docker
            client = docker.from_env()

            # List all networks
            networks = client.networks.list()

            # Filter and remove networks with names starting with the prefix
            for network in networks:
                if network.name.startswith(prefix):
                    network.remove()
                    logging.info(f"Network {network.name} removed successfully.")

        except docker.errors.NotFound:
            logging.info(f"One or more networks with prefix {prefix} not found.")
        except docker.errors.APIError:
            logging.info("Error interacting with Docker")
        except Exception:
            logging.info("Unexpected error")

    @classmethod
    def remove_containers_by_prefix(cls, prefix):
        """
        Removes all Docker containers whose names start with the given prefix.
        Containers are forcibly removed even if they are running.

        Args:
            prefix (str): Prefix string to match container names.

        Returns:
            None
        """
        try:
            # Connect to Docker client
            client = docker.from_env()

            containers = client.containers.list(all=True)  # `all=True` to include stopped containers

            # Iterate through containers and remove those with the matching prefix
            for container in containers:
                if container.name.startswith(prefix):
                    logging.info(f"Removing container: {container.name}")
                    container.remove(force=True)  # force=True to stop and remove if running
                    logging.info(f"Container {container.name} removed successfully.")

        except docker.errors.APIError:
            logging.exception("Error interacting with Docker")
        except Exception:
            logging.exception("Unexpected error")
