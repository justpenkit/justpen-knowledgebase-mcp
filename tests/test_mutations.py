"""Pure property mutation contract regressions."""

import pytest

from justpen_knowledgebase_mcp.mutations import merge_properties


def test_remove_sibling_and_merge_are_compatible():
    current = {"a": {"x": 1, "y": 2}}
    assert merge_properties(current, {"a": {"y": 3}}, ["/a/x"]) == {"a": {"y": 3}}
    assert current == {"a": {"x": 1, "y": 2}}


def test_empty_object_does_not_clear_and_child_removal_does():
    assert merge_properties({"a": {"x": 1}}, {"a": {}}, []) == {"a": {"x": 1}}
    assert merge_properties({"a": {"x": 1}}, {}, ["/a/x"]) == {"a": {}}


@pytest.mark.parametrize("patch", [{"a": 3}, {"a": None}, {"a": []}, {"a": {"x": 4}}])
def test_conflicting_replacement_rejected(patch):
    with pytest.raises(ValueError):
        merge_properties({"a": {"x": 1}}, patch, ["/a/x"])


def test_type_changes_null_arrays_and_escaped_keys():
    assert merge_properties({"a": 1, "b": [1], "~/": 3}, {"a": {"x": None}, "b": [False]}, ["/~0~1"]) == {
        "a": {"x": None},
        "b": [False],
    }
    assert merge_properties({}, {"a": True}, ["/missing"]) == {"a": True}


@pytest.mark.parametrize("pointer", ["", "a", "/~2", "/a/0", "/a/0/x"])
def test_invalid_pointer_and_array_element_removal(pointer):
    with pytest.raises(ValueError):
        merge_properties({"a": [1]}, {}, [pointer])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 2**63, -(2**63) - 1])
def test_recursive_number_limits(value):
    with pytest.raises(ValueError):
        merge_properties({}, {"a": [{"b": value}]}, [])


def test_byte_depth_and_valid_numeric_boundaries():
    assert merge_properties({}, {"a": -(2**63), "b": 2**63 - 1, "c": 1.5, "d": True}, [])["d"] is True
    with pytest.raises(ValueError):
        merge_properties({}, {"a": "x" * 65536}, [])
    tree = {}
    for _ in range(17):
        tree = {"a": tree}
    with pytest.raises(ValueError):
        merge_properties({}, tree, [])


@pytest.mark.parametrize("current", [{}, {"a": {"x": 1}}])
def test_missing_and_existing_object_sibling_write_have_same_remove_behavior(current):
    assert merge_properties(current, {"a": {"y": 2}}, ["/a/x"]) == {"a": {"y": 2}}


@pytest.mark.parametrize("current", [{"a": 1}, {"a": None}, {"a": []}])
def test_scalar_to_object_is_a_real_ancestor_replacement(current):
    with pytest.raises(ValueError):
        merge_properties(current, {"a": {"y": 2}}, ["/a/x"])
