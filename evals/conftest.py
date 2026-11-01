"""Keep pytest out of the fixture trees.

`evals/scenarios/<case>/repo/` holds deliberately half-finished projects: the dates module
fails its test, the calc test imports an uninstalled `src` package. A bare `pytest` at the
repository root would collect them and report errors that belong to the fixtures, not to this
repository. The scenarios are data for the runner, never tests of this project.
"""
collect_ignore_glob = ['scenarios/*']
