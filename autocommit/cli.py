"""Command-line interface for autocommit."""

import click

from autocommit import __version__


@click.group()
@click.version_option(version=__version__)
def main():
    """Automatically cluster and commit changes."""


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def run(path):
    click.echo(f"Analyzing changes in {path}...")
