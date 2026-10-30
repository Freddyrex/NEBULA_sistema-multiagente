import argparse
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))  # Parent directory where is the NEBULA module
import nebula
from app.deployer import Deployer
from nebula.config import defaults as _defaults
from nebula.controller.scenarios import ScenarioManagement

argparser = argparse.ArgumentParser(description="Controller of NEBULA platform", add_help=False)

argparser.add_argument(
    "-cp",
    "--controllerport",
    dest="controllerport",
    default=os.getenv("NEBULA_CONTROLLER_PORT", _defaults.CONTROLLER_PORT),
    help=f"Controller port (default: {_defaults.CONTROLLER_PORT})",
)

argparser.add_argument(
    "--grafanaport",
    dest="grafanaport",
    default=os.getenv("NEBULA_GRAFANA_PORT", _defaults.GRAFANA_PORT),
    help=f"Grafana port (default: {_defaults.GRAFANA_PORT})",
)

argparser.add_argument(
    "--lokiport",
    dest="lokiport",
    default=os.getenv("LOKI_PORT", _defaults.LOKI_PORT),
    help=f"Loki port (default: {_defaults.LOKI_PORT})",
)

argparser.add_argument(
    "--wafport",
    dest="wafport",
    default=os.getenv("NEBULA_WAF_PORT", _defaults.WAF_PORT),
    help=f"WAF port (default: {_defaults.WAF_PORT})",
)

argparser.add_argument(
    "-wp",
    "--webport",
    dest="webport",
    default=os.getenv("NEBULA_FRONTEND_PORT", _defaults.FRONTEND_PORT),
    help=f"Frontend port (default: {_defaults.FRONTEND_PORT})",
)

argparser.add_argument(
    "-sp",
    "--statsport",
    dest="statsport",
    default=os.getenv("NEBULA_STATISTICS_PORT", _defaults.STATISTICS_PORT),
    help=f"Statistics port (default: {_defaults.STATISTICS_PORT})",
)

argparser.add_argument(
    "-st",
    "--stop",
    dest="stop",
    nargs="?",
    const="all",  # If no argument is given, stop all
    default=None,
    help="Stop NEBULA platform or nodes only (use '--stop nodes' to stop only the nodes)",
)

argparser.add_argument("-s", "--simulation", action="store_false", dest="simulation", help="Run simulation")

argparser.add_argument(
    "-c",
    "--config",
    dest="config",
    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config"),
    help="Config directory path",
)

argparser.add_argument(
    "-d",
    "--database",
    dest="databases",
    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "databases"),
    help="Nebula databases path",
)

argparser.add_argument(
    "-l",
    "--logs",
    dest="logs",
    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
    help="Logs directory path",
)

argparser.add_argument(
    "-ce",
    "--certs",
    dest="certs",
    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs"),
    help="Certs directory path",
)

argparser.add_argument(
    "-e",
    "--env",
    dest="env",
    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    help=".env file path",
)

argparser.add_argument("-dev", "--developement", dest="developement", default=True, help="Nebula for devs")

argparser.add_argument(
    "-p",
    "--production",
    dest="production",
    action="store_true",
    default=False,
    help="Production mode",
)

argparser.add_argument(
    "-ad",
    "--advanced",
    dest="advanced_analytics",
    action="store_true",
    default=False,
    help="Advanced analytics",
)

argparser.add_argument(
    "-v",
    "--version",
    action="version",
    version="%(prog)s " + nebula.__version__,
    help="Show version",
)
argparser.add_argument(
    "-a",
    "--about",
    action="version",
    version="Created by Enrique Tomás Martínez Beltrán",
    help="Show author",
)

argparser.add_argument(
    "--set-password",
    dest="set_password",
    nargs="?",
    const="",
    default=None,
    metavar="PASSWORD",
    help="Set (or show) the admin password of the platform and exit",
)

argparser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help="Show help")

args = argparser.parse_args()

"""
Code for deploying the controller
"""
if __name__ == "__main__":
    if args.stop == "all":
        # `stop_all` termina el proceso con sys.exit(0): nunca debe caer en
        # el arranque de la plataforma (bug: un "stop" acababa arrancando NEBULA).
        Deployer.stop_all()
        sys.exit(0)
    elif args.stop == "nodes":
        ScenarioManagement.stop_nodes()
        sys.exit(0)
    elif args.set_password is not None:
        sys.exit(Deployer.set_admin_password(args.set_password or None, args.databases))

    Deployer(args).start()
