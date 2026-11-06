# Saturn Studio Design JSX -- the authoring reference

Saturn draws a design from **one JSX element** -- a scene description, not HTML.
Return a single root element and nothing else: no markdown fences, no comments,
no prose. Every attribute below is one Saturn's native engine actually reads;
anything else is ignored, so use only these words.

## Elements

| Element | What it is |
| --- | --- |
| `Frame` | A container. Holds children; the box for auto-layout. |
| `Rectangle` | A filled, optionally rounded shape. |
| `Ellipse` | A filled circle or oval. |
| `Line` | A one-pixel-thick rule between two points. |
| `Text` | A run of text. Put the words between the tags: `<Text>Hello</Text>`. |
| `Icon` | A labelled placeholder box (no glyph runs yet). |
| `Group` | A transparent frame that only groups children. |

Every element takes a `name` (the layer name shown in the rail).

## Size and position

- `w` / `h` -- a number (`w={640}`), or the tokens `"fill"` (take the parent's
  leftover space on that axis) or `"hug"` (shrink-wrap the content).
- `x` / `y` -- an explicit offset inside the parent. Give them only when you
  mean absolute placement; otherwise let auto-layout arrange the children.

## Paint

- `bg` or `fill` -- the shape's colour, e.g. `bg="#101418"`.
- `color` -- the text colour on a `Text` node.
- `stroke` (+ `strokeWidth`) -- a border, e.g. `stroke="#2BD6FF" strokeWidth={2}`.
- `rounded` (or `radius`) -- corner radius in pixels.
- `opacity` -- `0` to `1`.

## Text

On a `Text` element: `size` (points), `weight` (100-900), `font` / `family`,
and `textAlign` (`left` | `center` | `right` | `justify`).

## Auto-layout (the whole point of a Frame)

Put these on a container to arrange its children with real arithmetic instead
of hand-placing every pixel:

- `flex="row"` or `flex="col"` -- the direction. Omit it to stack children.
- `gap={16}` -- space between children along the main axis.
- `p={24}` -- padding on all sides (`px` / `py` for the two axes).
- `justify` -- along the main axis: `start` | `center` | `end` | `between`.
- `items` -- across the axis: `start` | `center` | `end` | `stretch`.
- `wrap` -- let children flow onto new lines.

On a child inside a flex container:

- `grow={1}` -- take a share of the leftover main-axis space.
- `alignSelf` -- override the parent's cross-axis alignment for this one child.
- `pos="absolute"` -- leave the flow and keep its own `x` / `y`.

## Example

```jsx
<Frame name="Card" w={640} h="hug" flex="col" gap={16} p={24} bg="#101418" rounded={12}>
  <Text name="Headline" size={34} weight={700} color="#F0B429">Saturn drew this</Text>
  <Frame name="Bar" w="fill" h={8} bg="#2BD6FF" rounded={4} />
  <Text name="Caption" size={16} color="#C8D3E0">one frame, real pixels</Text>
</Frame>
```

A `w="fill"` child inside a padded column lands at the padding inset and spans
the inner width -- the flex pass resolves it against real leftover space, so you
never have to compute coordinates by hand.
