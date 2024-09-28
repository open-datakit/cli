import os
import time
import shutil
import json
import re
import pickle
import typer
import docker
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
from ast import literal_eval
from typing import Optional, Any
from typing_extensions import Annotated
from rich import print
from rich.panel import Panel
from tabulate import tabulate
from opendatapy.datapackage import (
    ExecutionError,
    ResourceError,
    execute_datapackage,
    execute_view,
    init_resource,
    load_resource_by_variable,
    write_resource,
    load_run_configuration,
    write_run_configuration,
    load_variable,
    load_variable_signature,
    load_datapackage_configuration,
    write_datapackage_configuration,
    load_algorithm,
    write_algorithm,
    get_algorithm_name,
    RUN_DIR,
    RELATIONSHIPS_FILE,
    VIEW_ARTEFACTS_DIR,
)
from opendatapy.helpers import find_by_name, find


app = typer.Typer()


client = docker.from_env()


# Assume we are always at the datapackage root
# TODO: Validate we actually are, and that this is a datapackage
DATAPACKAGE_PATH = os.getcwd()  # Root datapackage path
CONFIG_FILE = f"{DATAPACKAGE_PATH}/.opends"
RUN_EXTENSION = ".run"


# Helpers


def dumb_str_to_type(value) -> Any:
    """Parse a string to any Python type"""
    # Stupid workaround for Typer not supporting Union types :<
    try:
        return literal_eval(value)
    except ValueError:
        if value.lower() == "true":
            return True
        elif value.lower() == "false":
            return False
        else:
            return value


def get_default_algorithm() -> str:
    """Return the default algorithm for the current datapackage"""
    return load_datapackage_configuration(base_path=DATAPACKAGE_PATH)[
        "algorithms"
    ][0]


def load_config():
    """Load CLI configuration file"""
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def get_active_run():
    try:
        return load_config()["run"]
    except FileNotFoundError:
        print('[red]No active run is set. Have you run "opends init"?[/red]')
        exit(1)


def write_config(run_name):
    """Write updated CLI configuration file"""
    with open(CONFIG_FILE, "w") as f:
        json.dump({"run": run_name}, f, indent=2)


def run_exists(run_name):
    """Check if specified run exists"""
    run_dir = RUN_DIR.format(base_path=DATAPACKAGE_PATH, run_name=run_name)
    return os.path.exists(run_dir) and os.path.isdir(run_dir)


def get_full_run_name(run_name):
    """Validate and return full run name"""
    if run_name is not None:
        # Check the run_name matches the pattern [algorithm].[name]
        pattern = re.compile(r"^([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)$")

        if not pattern.match(run_name):
            print(f'[red]"{run_name}" is not a valid run name[/red]')
            print(
                "[red]Run names must match the format: "
                r"\[algorithm].\[name][/red]"
            )
            exit(1)

        algorithm_name = get_algorithm_name(run_name)
        datapackage_algorithms = load_datapackage_configuration(
            base_path=DATAPACKAGE_PATH
        )["algorithms"]

        if get_algorithm_name(run_name) not in datapackage_algorithms:
            print(
                f'[red]"{algorithm_name}" is not a valid datapacakge '
                "algorithm[/red]"
            )
            print(
                "[red]Available datapackage algorithms: "
                f"{datapackage_algorithms}[/red]"
            )
            exit(1)

        return run_name + RUN_EXTENSION
    else:
        return get_default_algorithm() + RUN_EXTENSION


def execute_relationship(run_name: str, variable_name: str) -> None:
    """Execute any relationships applied to the given source variable"""
    # Load run configuration for modification
    run = load_run_configuration(run_name)

    # Load associated relationship
    with open(
        RELATIONSHIPS_FILE.format(
            base_path=DATAPACKAGE_PATH,
            algorithm_name=get_algorithm_name(run_name),
        ),
        "r",
    ) as f:
        relationship = find(
            json.load(f)["relationships"], "source", variable_name
        )

    # Apply relationship rules
    for rule in relationship["rules"]:
        if rule["type"] == "value":
            # Check if this rule applies to current run configuration state

            # Get source variable value
            value = load_variable(
                run_name=run_name,
                variable_name=variable_name,
                base_path=DATAPACKAGE_PATH,
            )["value"]

            # If the source variable value matches the rule value, execute
            # the relationship
            if value in rule["values"]:
                for target in rule["targets"]:
                    if "disabled" in target:
                        # Set target variable disabled value
                        target_variable = load_variable(
                            run_name=run_name,
                            variable_name=target["name"],
                            base_path=DATAPACKAGE_PATH,
                        )

                        target_variable["disabled"] = target["disabled"]

                    if target["type"] == "resource":
                        # Set target resource data and schema
                        target_resource = load_resource_by_variable(
                            run_name=run["name"],
                            variable_name=target["name"],
                            base_path=DATAPACKAGE_PATH,
                            as_dict=True,
                        )

                        if target.get("data") is not None:
                            target_resource["data"] = target["data"]

                        if target.get("schema") is not None:
                            target_resource["schema"] = target["schema"]

                        write_resource(
                            run_name=run["name"],
                            resource=target_resource,
                            base_path=DATAPACKAGE_PATH,
                        )
                    elif target["type"] == "value":
                        # Set target variable value
                        target_variable = load_variable(
                            run_name=run_name,
                            variable_name=target["name"],
                            base_path=DATAPACKAGE_PATH,
                        )

                        if target.get("value") is not None:
                            target_variable["value"] = target["value"]

                        if target.get("metaschema") is not None:
                            target_variable["metaschema"] = target[
                                "metaschema"
                            ]
                    else:
                        raise NotImplementedError(
                            (
                                'Only "resource" and "value" type rule '
                                "targets are implemented"
                            )
                        )

        else:
            raise NotImplementedError("Only value-based rules are implemented")

    # Write modified run configuration
    write_run_configuration(run, base_path=DATAPACKAGE_PATH)


# Commands


@app.command()
def init(
    run_name: Annotated[
        Optional[str],
        typer.Argument(
            help=(
                "Name of the run you want to initialise in the format "
                "[algorithm].[run name]"
            )
        ),
    ] = None,
) -> None:
    """Initialise a datapackage run"""
    run_name = get_full_run_name(run_name)

    # Check directory doesn't already exist
    if run_exists(run_name):
        print(f"[red]{run_name} already exists[/red]")
        exit(1)

    # Create run directory
    run_dir = RUN_DIR.format(base_path=DATAPACKAGE_PATH, run_name=run_name)
    os.makedirs(f"{run_dir}/resources")
    os.makedirs(f"{run_dir}/views")
    print(f"[bold]=>[/bold] Created run directory: {run_dir}")

    algorithm_name = get_algorithm_name(run_name)
    algorithm = load_algorithm(algorithm_name, base_path=DATAPACKAGE_PATH)

    # Generate default run configuration
    run = {
        "name": run_name,
        "title": f"Run configuration for {algorithm_name}",
        "profile": "opends-run",
        "algorithm": f"{algorithm_name}",
        "container": f'{algorithm["container"]}',
        "data": {
            "inputs": [],
            "outputs": [],
        },
    }

    # Create run configuration and initialise resources
    for variable in algorithm["signature"]["inputs"]:
        # Add variable defaults to run configuration
        run["data"]["inputs"].append(
            {
                "name": variable["name"],
                **variable["default"],
            }
        )

        # Initialise associated resources
        if variable["type"] == "resource":
            resource_name = variable["default"]["resource"]

            init_resource(
                run_name=run["name"],
                resource_name=resource_name,
                base_path=DATAPACKAGE_PATH,
            )

            print(f"[bold]=>[/bold] Generated input resource: {resource_name}")

    for variable in algorithm["signature"]["outputs"]:
        # Add variable defaults to run configuration
        run["data"]["outputs"].append(
            {
                "name": variable["name"],
                **variable["default"],
            }
        )

        # Initialise associated resources
        if variable["type"] == "resource":
            resource_name = variable["default"]["resource"]

            init_resource(
                run_name=run["name"],
                resource_name=resource_name,
                base_path=DATAPACKAGE_PATH,
            )

            print(f"[bold]=>[/bold] Generated input resource: {resource_name}")

    # Write generated configuration
    write_run_configuration(run, base_path=DATAPACKAGE_PATH)

    print(f"[bold]=>[/bold] Generated default run configuration: {run_name}")

    # Add default run to datapackage.json
    datapackage = load_datapackage_configuration(base_path=DATAPACKAGE_PATH)
    datapackage["runs"].append(run_name)
    write_datapackage_configuration(datapackage, base_path=DATAPACKAGE_PATH)

    # Write current run name to config
    write_config(run_name)


@app.command()
def set_run(
    run_name: Annotated[
        Optional[str],
        typer.Argument(help="Name of the run you want to enable"),
    ] = None,
) -> None:
    """Set the active run"""
    run_name = get_full_run_name(run_name)

    if run_exists(run_name):
        # Set to active run
        write_config(run_name)
    else:
        print(f"[red]{run_name} does not exist[/red]")


@app.command()
def get_run() -> None:
    """Get the active run"""
    print(f"[bold]{get_active_run()}[/bold]")


@app.command()
def run() -> None:
    """Execute the active run"""
    run_name = get_active_run()

    # Execute algorithm container and print any logs
    print(f"[bold]=>[/bold] Executing [bold]{run_name}[/bold]")

    try:
        logs = execute_datapackage(
            client,
            run_name,
            base_path=DATAPACKAGE_PATH,
        )
    except ExecutionError as e:
        print(
            Panel(
                e.logs,
                title="[bold red]Execution error[/bold red]",
            )
        )
        print("[red]Container execution failed[/red]")
        exit(1)

    if logs:
        print(
            Panel(
                logs,
                title="[bold]Execution container output[/bold]",
            )
        )

    print(f"[bold]=>[/bold] Executed [bold]{run_name}[/bold] successfully")


@app.command()
def show(
    variable_name: Annotated[
        str,
        typer.Argument(
            help="Name of variable to print",
            show_default=False,
        ),
    ],
) -> None:
    """Print a variable value"""
    run_name = get_active_run()

    # Load algorithum signature to check variable type
    signature = load_variable_signature(
        run_name=run_name,
        variable_name=variable_name,
        base_path=DATAPACKAGE_PATH,
    )

    if signature["type"] == "resource":
        # Variable is a tabular data resource
        resource = load_resource_by_variable(
            run_name=run_name,
            variable_name=variable_name,
            base_path=DATAPACKAGE_PATH,
        )

        print(
            tabulate(
                resource.to_dict()["data"],
                headers="keys",
                tablefmt="rounded_grid",
            )
        )
    else:
        # Variable is a simple string/number/bool value
        variable = load_variable(
            run_name=run_name,
            variable_name=variable_name,
            base_path=DATAPACKAGE_PATH,
        )

        print(
            Panel(
                str(variable["value"]),
                title=f"{variable_name}",
                expand=False,
            )
        )


@app.command()
def view(
    view_name: Annotated[
        str,
        typer.Argument(
            help="The name of the view to render", show_default=False
        ),
    ],
) -> None:
    """Render a view locally"""
    run_name = get_active_run()

    print(f"[bold]=>[/bold] Generating [bold]{view_name}[/bold] view")

    try:
        logs = execute_view(
            docker_client=client,
            run_name=run_name,
            view_name=view_name,
            base_path=DATAPACKAGE_PATH,
        )
    except ResourceError as e:
        print("[red]" + e.message + "[/red]")
        exit(1)
    except ExecutionError as e:
        print(
            Panel(
                e.logs,
                title="[bold red]View execution error[/bold red]",
            )
        )
        print("[red]View execution failed[/red]")
        exit(1)

    if logs:
        print(
            Panel(
                logs,
                title="[bold]View container output[/bold]",
            )
        )

    print(
        f"[bold]=>[/bold] Successfully generated [bold]{view_name}[/bold] view"
    )

    print(
        "[blue][bold]=>[/bold] Loading interactive view in web browser[/blue]"
    )

    matplotlib.use("WebAgg")

    with open(
        VIEW_ARTEFACTS_DIR.format(
            base_path=DATAPACKAGE_PATH, run_name=run_name
        )
        + f"/{view_name}.p",
        "rb",
    ) as f:
        # NOTE: The matplotlib version in CLI must be >= the version of
        # matplotlib used to generate the plot (which is chosen by the user)
        # So the CLI should be kept up to date at all times

        # Load matplotlib figure
        pickle.load(f)

    plt.show()


@app.command()
def load(
    variable_name: Annotated[
        str,
        typer.Argument(
            help="Name of variable to populate",
            show_default=False,
        ),
    ],
    path: Annotated[
        str,
        typer.Argument(
            help="Path to data to ingest (xml, csv)", show_default=False
        ),
    ],
) -> None:
    """Load data into configuration variable"""
    run_name = get_active_run()

    # Load resource into TabularDataResource object
    resource = load_resource_by_variable(
        run_name=run_name,
        variable_name=variable_name,
        base_path=DATAPACKAGE_PATH,
    )

    # Read CSV into resource
    print(f"[bold]=>[/bold] Reading {path}")
    resource.data = pd.read_csv(path)

    # Write to resource
    write_resource(
        run_name=run_name, resource=resource, base_path=DATAPACKAGE_PATH
    )

    print("[bold]=>[/bold] Resource successfully loaded!")


@app.command()
def set(
    variable_ref: Annotated[
        str,
        typer.Argument(
            help=(
                "Either a variable name, or a parameter reference in the "
                "format [resource name].[parameter name]"
            ),
            show_default=False,
        ),
    ],
    variable_value: Annotated[
        str,  # Workaround for union types not being supported by Typer yet
        # Union[str, int, float, bool],
        typer.Argument(
            help="Value to set",
            show_default=False,
        ),
    ],
) -> None:
    """Set a variable value"""
    run_name = get_active_run()

    # Parse value (workaround for Typer not supporting Union types :<)
    variable_value = dumb_str_to_type(variable_value)

    if "." in variable_ref:
        # Variable reference is a parameter reference

        # Check the variable_ref matches the pattern [resource].[param]
        pattern = re.compile(r"^([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)$")

        if not pattern.match(variable_ref):
            print(
                "[red]Variable name argument must be either a variable name "
                "or a parameter reference in the format "
                r"\[resource name].\[param name][/red]"
            )
            exit(1)

        # Parse variable and param names
        variable_name, param_name = variable_ref.split(".")

        # Load param resource
        resource = load_resource_by_variable(
            run_name=run_name,
            variable_name=variable_name,
            base_path=DATAPACKAGE_PATH,
        )

        # Check it's a param resource
        if resource.profile != "parameter-tabular-data-resource":
            print(
                f"[red]Resource [bold]{resource.name}[/bold] is not of type "
                '"parameters"[/red]'
            )
            exit(1)

        # If data is not populated, something has gone wrong
        if not resource:
            print(
                f'[red]Parameter resource [bold]{resource.name}[/bold] "data" '
                'field is empty. Try running "opends reset"?[/red]'
            )
            exit(1)

        print(
            f"[bold]=>[/bold] Setting parameter [bold]{param_name}[/bold] to "
            f"value [bold]{variable_value}[/bold]"
        )

        # Set parameter value (initial guess)
        try:
            # This will generate a key error if param_name doesn't exist
            # The assignment doesn't unfortunately
            resource.data.loc[param_name]  # Ensure param_name row exists
            resource.data.loc[param_name, "init"] = variable_value
        except KeyError:
            print(
                f'[red]Could not find parameter "{param_name}" in resource '
                f"[bold]{resource.name}[/bold][/red]"
            )
            exit(1)

        # Write resource
        write_resource(
            run_name=run_name, resource=resource, base_path=DATAPACKAGE_PATH
        )

        print(
            f"[bold]=>[/bold] Successfully set parameter [bold]{param_name}"
            f"[/bold] value to [bold]{variable_value}[/bold] in parameter "
            f"resource [bold]{resource.name}[/bold]"
        )
    else:
        # Variable reference is a simple variable name
        variable_name = variable_ref

        # Load variable signature
        signature = load_variable_signature(
            run_name, variable_name, base_path=DATAPACKAGE_PATH
        )

        # Convenience dict mapping opends types to Python types
        type_map = {
            "string": str,
            "boolean": bool,
            "number": float | int,
        }

        # Check the value is of the expected type for this variable
        # Raise some helpful errors
        if signature.get("profile") == "tabular-data-resource":
            print('[red]Use command "load" for tabular data resource[/red]')
            exit(1)
        elif signature.get("profile") == "parameter-tabular-data-resource":
            print('[red]Use command "set-param" for parameter resource[/red]')
            exit(1)
        # Specify False as fallback value here to avoid "None"s leaking through
        elif type_map.get(signature["type"], False) != type(variable_value):
            print(
                f"[red]Variable value must be of type {signature['type']}"
                "[/red]"
            )
            exit(1)

        # If this variable has an enum, check the value is allowed
        if signature.get("enum", False):
            allowed_values = [i["value"] for i in signature["enum"]]
            if variable_value not in allowed_values:
                print(
                    f"[red]Variable value must be one of {allowed_values}"
                    "[/red]"
                )
                exit(1)

        # Check if nullable
        if not signature["null"]:
            if not variable_value:
                print("[red]Variable value cannot be null[/red]")
                exit(1)

        # Load run configuration
        run = load_run_configuration(run_name, base_path=DATAPACKAGE_PATH)

        # Set variable value
        find_by_name(
            run["data"]["inputs"] + run["data"]["outputs"], variable_name
        )["value"] = variable_value

        # Write configuration
        write_run_configuration(run, base_path=DATAPACKAGE_PATH)

        # Execute any relationships applied to this variable value
        execute_relationship(
            run_name=run_name,
            variable_name=variable_name,
        )

        print(
            f"[bold]=>[/bold] Successfully set [bold]{variable_name}[/bold] "
            "variable"
        )

    show(variable_name)


@app.command()
def reset():
    """Reset datapackage to clean state

    Removes all run outputs and resets configurations to default
    """
    # Remove all run directories
    for f in os.scandir(DATAPACKAGE_PATH):
        if f.is_dir() and f.path.endswith(".run"):
            print(f"[bold]=>[/bold] Deleting [bold]{f.name}[/bold]")
            shutil.rmtree(f.path)

    # Remove all run references from datapackage.json
    datapackage = load_datapackage_configuration(base_path=DATAPACKAGE_PATH)
    datapackage["runs"] = []
    write_datapackage_configuration(datapackage, base_path=DATAPACKAGE_PATH)

    # Remove CLI config
    if os.path.exists(CONFIG_FILE):
        os.remove(CONFIG_FILE)


@app.command()
def new(
    algorithm_name: Annotated[
        str,
        typer.Argument(
            help="Name of the algorithm to generate",
            show_default=False,
        ),
    ],
) -> None:
    """Generate a new datapackage and algorithm scaffold"""
    # Create new datapackage directory
    datapackage_name = f"{algorithm_name}-datapackage"
    datapackage_dir = f"{DATAPACKAGE_PATH}/{datapackage_name}"
    algorithm_dir = f"{datapackage_dir}/{algorithm_name}"

    if not os.path.exists(datapackage_dir):
        os.makedirs(datapackage_dir)
        os.makedirs(algorithm_dir)
    else:
        print(
            f'[red]Directory named "{datapackage_name}" already exists[/red]'
        )
        exit(1)

    current_time = int(time.time())

    datapackage = {
        "title": "New datapackage",
        "description": "A new datapackage",
        "profile": "opends-analysis-datapackage",
        "algorithms": [algorithm_name],
        "runs": [],
        "repository": {
            "type": "git",
            "url": "https://github.com/opendatastudio/example-datapackage",
            "name": "opendatastudio/example-datapackage",
        },
        "created": current_time,
        "updated": current_time,
    }

    algorithm = {
        "name": algorithm_name,
        "title": "New algorithm",
        "profile": "opends-algorithm",
        "code": "algorithm.py",
        "container": "opends/python-run-base:v1",
        "signature": [
            {
                "name": "input",
                "title": "Input",
                "description": "An input variable",
                "type": "number",
                "null": False,
                "default": {"value": 42},
            },
            {
                "name": "output",
                "title": "Output",
                "description": "An output variable",
                "type": "number",
                "null": True,
                "default": {"value": None},
            },
        ],
        "relationships": [],
    }

    algorithm_code = '''def main(input, output):
    """A new algorithm"""

    return {
        "output": input**2,
    }
    '''

    write_datapackage_configuration(datapackage, base_path=datapackage_dir)
    write_algorithm(algorithm, base_path=datapackage_dir)
    with open(f"{datapackage_dir}/{algorithm_name}/algorithm.py", "x") as f:
        f.write(algorithm_code)

    print(
        f"[bold]=>[/bold] Successfully created [bold]{datapackage_name}[/bold]"
    )


if __name__ == "__main__":
    app()
