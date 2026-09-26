"""Evaluate the compiled registry expression locally; never call Azure APIs."""
import ast
import json
import re
import shutil
import subprocess

import pytest
from test_deployment import ENV, ROOT, helper, live

SERVER = "example.azurecr.io"
CANONICAL = {"server": SERVER, "identity": "system"}
AZURE_BINDING = {**CANONICAL, "username": None, "passwordSecretRef": None}
OTHER = {"server": "other.azurecr.io", "identity": None,
         "username": "other-user", "passwordSecretRef": "other-password"}


@pytest.fixture(scope="module")
def registry_expression():
    if not shutil.which("az"):
        pytest.skip("Azure CLI with local Bicep compiler required")
    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(ROOT / "infra/main.bicep"), "--stdout"],
        capture_output=True, text=True, timeout=180, check=True,
    )
    template = json.loads(result.stdout)
    app = next(r for r in template["resources"] if r["type"] == "Microsoft.App/containerApps")
    expression = app["properties"]["configuration"]["registries"]
    # These ARM function names are Python keywords; all other syntax in this
    # small expression is also valid Python AST. No eval or Azure deployment.
    expression = re.sub(r"\b(if|not|lambda)\(", r"arm_\1(", expression[1:-1])
    return ast.parse(expression, mode="eval").body


def evaluate(node, params, scope=None):
    """Only the ARM subset used by registries; unknown constructs fail closed."""
    scope = scope or {}
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        return evaluate(node.value, params, scope)[node.attr]
    assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name), ast.dump(node)
    name = node.func.id
    if name == "reference":
        # Substitute only the registry resource's runtime loginServer lookup.
        assert "Microsoft.ContainerRegistry/registries" in ast.unparse(node)
        return {"loginServer": SERVER}
    if name == "arm_if":
        branch = node.args[1] if evaluate(node.args[0], params, scope) else node.args[2]
        return evaluate(branch, params, scope)
    if name == "arm_lambda":
        key = evaluate(node.args[0], params, scope)
        return lambda item: evaluate(node.args[1], params, {**scope, key: item})
    args = [evaluate(arg, params, scope) for arg in node.args]
    if name == "union":
        # ARM array union compares entire objects, including null properties.
        result = []
        for array in args:
            for item in array:
                if item not in result:
                    result.append(item)
        return result
    functions = {
        "parameters": lambda key: params[key],
        "lambdaVariables": lambda key: scope[key],
        "createArray": lambda *items: list(items),
        "createObject": lambda *items: dict(zip(items[::2], items[1::2], strict=True)),
        "concat": lambda *arrays: [item for array in arrays for item in array],
        "filter": lambda array, predicate: [item for item in array if predicate(item)],
        "equals": lambda left, right: left == right,
        "arm_not": lambda value: not value,
    }
    assert name in functions, f"Unsupported ARM function: {name}"
    return functions[name](*args)


@pytest.mark.parametrize("use_acr,registries", [
    (True, [AZURE_BINDING, OTHER]),
    (True, [CANONICAL, OTHER]),
    (True, [{"server": SERVER, "username": "old-user", "passwordSecretRef": "old-secret"}, OTHER]),
    (True, []),
    (False, []),
    (False, [AZURE_BINDING, OTHER]),
])
def test_compiled_registry_binding_is_unique_and_preserves_other_servers(
    registry_expression, use_acr, registries,
):
    d = helper()
    app = live(f"{SERVER}/api:real-sha" if use_acr else d.PLACEHOLDER)
    app["properties"]["configuration"]["registries"] = registries
    # Exercise the actual deploy helper and ARM serialization, not hand-built
    # runtimeConfig or a forced false/bootstrap branch.
    parameters = d.parameters(app, [], ENV if use_acr else {}, "eastus2")
    params = {key: value["value"] for key, value in d.arm_parameters(parameters)["parameters"].items()}
    assert params["useAcrImage"] is use_acr
    assert params["runtimeConfig"]["registries"] == registries
    result = evaluate(registry_expression, params)
    expected = ([r for r in registries if r["server"] != SERVER] + [CANONICAL]
                if use_acr else registries)
    assert len({r["server"] for r in result}) == len(result), "duplicate registry server"
    assert sorted(result, key=lambda r: r["server"]) == sorted(expected, key=lambda r: r["server"])
    # Replaying the emitted bindings is idempotent as well.
    params["runtimeConfig"]["registries"] = result
    assert evaluate(registry_expression, params) == result
