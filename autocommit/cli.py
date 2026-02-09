"""Command-line interface for autocommit."""

import click

from autocommit import __version__


@click.group()
@click.version_option(version=__version__)
def main():
    


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def run(path):
    click.echo(f"Analyzing changes in {path}...")
