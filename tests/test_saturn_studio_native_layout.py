"""Milestone 2 -- the flex pass, tested at the Qt-free model layer.

The whole point of auto-layout is that a ``render`` no longer has to *say* it
merely stacked things: give a container a direction and it arranges its
children with real arithmetic. These tests drive :meth:`scene.Node.run_layout`
directly (through ``jsx.build``) so the numbers are the model's own, with no
painter shaping them -- the exact same pass a live Qt canvas runs.

The gate the engine plan names is here: a ``flex="col"`` frame 640 wide with 24
of padding and a ``w="fill"`` child must put that child at x=24 and 592 wide.
That only happens if the fill resolved against real leftover space, not a
guessed column.
"""

import pytest

from saturn_studio.native import jsx, scene


def build(markup):
    root, warnings = jsx.build(markup)
    return root, warnings


# --------------------------------------------------------------------------- #
# The fill gate (the plan's acceptance numbers)
# --------------------------------------------------------------------------- #

LIVE_CHECK = (
    '<Frame name="Live check" w={640} h={360} flex="col" gap={16} p={24}/>'
)


def test_fill_child_takes_the_padding_box_exactly():
    root, warnings = build(
        '<Frame name="Live check" w={640} h={360} bg="#101418" flex="col" '
        'gap={16} p={24} rounded={12}>'
        '<Text name="Headline" size={34}>Saturn drew this</Text>'
        '<Frame name="Bar" w="fill" h={8} bg="#2BD6FF" rounded={4} />'
        "</Frame>"
    )
    bar = root.children[1]
    assert bar.x == 24.0                                   # one padding in
    assert abs(bar.width - 592.0) < 0.01                   # 640 - 24 - 24
    assert bar.y > 24.0                                     # below the headline
    assert warnings == []                                  # flex laid it out


def test_declaring_flex_removes_the_stack_warning():
    _root, warnings = build(LIVE_CHECK)
    assert warnings == []


def test_no_flex_still_stacks_and_warns_by_name():
    root, warnings = build(
        '<Frame name="Plain" w={200} h={200}>'
        '<Rectangle name="A" w={50} h={20} bg="#fff"/>'
        '<Rectangle name="B" w={50} h={20} bg="#fff"/>'
        "</Frame>"
    )
    assert any("stacked" in w and "A" in w for w in warnings)
    a, b = root.children
    assert a.y < b.y and a.x == b.x                        # stacked in a column


# --------------------------------------------------------------------------- #
# Direction, gap and the main axis
# --------------------------------------------------------------------------- #

def test_row_runs_left_to_right_and_column_top_to_bottom():
    row, _ = build(
        '<Frame name="R" w={300} h={100} flex="row" gap={10} p={0}>'
        '<Rectangle w={50} h={50} bg="#fff"/><Rectangle w={50} h={50} bg="#fff"/>'
        "</Frame>"
    )
    left, right = row.children
    assert left.x == 0 and right.x == 60                   # 50 + 10 gap

    col, _ = build(
        '<Frame name="C" w={100} h={300} flex="col" gap={10} p={0}>'
        '<Rectangle w={50} h={50} bg="#fff"/><Rectangle w={50} h={50} bg="#fff"/>'
        "</Frame>"
    )
    top, bottom = col.children
    assert top.y == 0 and bottom.y == 60


def test_two_fill_children_split_the_leftover_space():
    root, _ = build(
        '<Frame name="S" w={400} h={100} flex="row" gap={0}>'
        '<Rectangle w="fill" h={100} bg="#fff"/><Rectangle w="fill" h={100} bg="#fff"/>'
        "</Frame>"
    )
    a, b = root.children
    assert abs(a.width - 200) < 0.01 and abs(b.width - 200) < 0.01
    assert abs(b.x - 200) < 0.01


# --------------------------------------------------------------------------- #
# Alignment on both axes
# --------------------------------------------------------------------------- #

def test_space_between_pushes_first_and_last_to_the_edges():
    root, _ = build(
        '<Frame name="J" w={400} h={100} flex="row" justify="between" p={0}>'
        '<Rectangle w={50} h={50} bg="#fff"/><Rectangle w={50} h={50} bg="#fff"/>'
        '<Rectangle w={50} h={50} bg="#fff"/></Frame>'
    )
    first, _mid, last = root.children
    assert first.x == 0 and abs(last.x - 350) < 0.01       # 400 - 50


def test_center_on_main_axis_offsets_the_block():
    root, _ = build(
        '<Frame name="K" w={200} h={100} flex="row" justify="center" p={0}>'
        '<Rectangle w={50} h={50} bg="#fff"/></Frame>'
    )
    assert abs(root.children[0].x - 75) < 0.01            # (200 - 50) / 2


def test_stretch_across_the_column_makes_children_full_width():
    root, _ = build(
        '<Frame name="T" w={300} h={200} flex="col" items="stretch" p={0}>'
        '<Rectangle w={10} h={20} bg="#fff"/></Frame>'
    )
    assert abs(root.children[0].width - 300) < 0.01


def test_align_self_overrides_the_parent_for_one_child():
    root, _ = build(
        '<Frame name="A" w={300} h={200} flex="col" p={0}>'
        '<Rectangle w={10} h={20} bg="#fff"/>'
        '<Rectangle h={20} w={10} alignSelf="stretch" bg="#fff"/></Frame>'
    )
    normal, stretched = root.children
    assert normal.width == 10 and abs(stretched.width - 300) < 0.01


# --------------------------------------------------------------------------- #
# Hug sizing and nesting
# --------------------------------------------------------------------------- #

def test_hug_container_shrinks_to_wrap_its_content():
    root, _ = build(
        '<Frame name="Outer" w={500} h={120} flex="row" gap={0} p={0}>'
        '<Frame name="Inner" w="hug" h={100} flex="row" gap={0}>'
        '<Rectangle w={40} h={40} bg="#fff"/><Rectangle w={40} h={40} bg="#fff"/>'
        "</Frame></Frame>"
    )
    inner = root.children[0]
    assert abs(inner.width - 80) < 0.01                     # hugs the two boxes


def test_nested_flex_lays_out_inner_against_resolved_outer():
    root, _ = build(
        '<Frame name="Card" w={300} h={200} flex="col" p={20} gap={10}>'
        '<Frame name="Body" w="fill" h={100} flex="row" p={0} gap={5}>'
        '<Rectangle w="fill" h={100} bg="#fff"/><Rectangle w={20} h={100} bg="#fff"/>'
        "</Frame></Frame>"
    )
    body = root.children[0]
    assert abs(body.width - 260) < 0.01                     # 300 - 2*20 padding
    wide, fixed = body.children
    assert abs(wide.width - 235) < 0.01                     # 260 - 20 - 5 gap
    assert abs(fixed.x - 240) < 0.01                        # 235 + 5


# --------------------------------------------------------------------------- #
# Absolute positioning
# --------------------------------------------------------------------------- #

def test_absolute_child_keeps_its_own_coordinates_and_leaves_the_flow():
    root, _ = build(
        '<Frame name="Stack" w={300} h={300} flex="col" gap={10} p={0}>'
        '<Rectangle w={50} h={50} bg="#fff"/>'
        '<Rectangle name="Pin" pos="absolute" x={100} y={100} w={30} h={30} bg="#fff"/>'
        '<Rectangle w={50} h={50} bg="#fff"/></Frame>'
    )
    pin = root.children[1]
    assert (pin.x, pin.y) == (100.0, 100.0)                # exactly where it said
    last = root.children[2]
    assert last.y == 60                                      # only 2 flowed children


# --------------------------------------------------------------------------- #
# The layout is real data: serialized and reported
# --------------------------------------------------------------------------- #

def test_layout_survives_the_dict_round_trip():
    root, _ = build(
        '<Frame name="L" w={300} h={200} flex="col" gap={8} p={12} '
        'justify="between" items="center">'
        '<Frame name="C" w="fill" h={20} bg="#fff"/></Frame>'
    )
    restored = scene.Node.from_dict(root.to_dict())
    assert restored.layout.mode == scene.FLEX_COL
    assert restored.layout.gap == 8 and restored.layout.pad_left == 12
    assert restored.layout.primary_align == scene.ALIGN_SPACE
    assert restored.layout.counter_align == scene.ALIGN_CENTER
    child = restored.children[0]
    assert child.sizing_horizontal == scene.SIZING_FILL


def test_read_view_reports_the_figma_layout_vocabulary():
    root, _ = build(LIVE_CHECK.replace("/>", ">"
        '<Frame name="Bar" w="fill" h={8} /></Frame>'))
    js = root.to_json()
    assert js["layoutMode"] == scene.FLEX_COL
    assert js["itemSpacing"] == 16 and js["paddingLeft"] == 24
    assert js["layoutWrap"] == "NO_WRAP"
    bar = js["children"][0]
    assert bar["layoutSizingHorizontal"] == scene.SIZING_FILL


def test_a_plain_box_reports_no_layout_keys_at_all():
    root, _ = build('<Frame name="Plain" w={100} h={100} bg="#fff"/>')
    js = root.to_json()
    assert "layoutMode" not in js and "itemSpacing" not in js
