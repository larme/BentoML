import click

from bentoml import __version__

from .bento_management import add_bento_management_commands
from .bento_server import add_serve_command
from .click_utils import BentoMLCommandGroup
from .containerize import add_containerize_command
from .model_store import models


def create_bentoml_cli():
    @click.group(cls=BentoMLCommandGroup)
    @click.version_option(version=__version__)
    def cli():
        """BentoML CLI"""

    # Add top-level CLI commands
    add_bento_management_commands(cli)
    add_serve_command(cli)
    add_containerize_command(cli)

    # Add "models" sub commands
    cli.add_command(models)

    return cli


cli = create_bentoml_cli()


if __name__ == "__main__":
    cli()
