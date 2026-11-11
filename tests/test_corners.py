"""Rounded corners of cards, tabs and buttons: the corner pieces that are laid over the square Tk widgets."""
import ui_widgets

OUTER, FILL, BORDER = "#1b1d21", "#24272d", "#3a3f48"


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def dist(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


def test_four_corners_of_the_requested_size():
    imgs = ui_widgets.corner_images(8, OUTER, FILL, BORDER)
    assert len(imgs) == 4 and all(im.size == (8, 8) for im in imgs)


def test_outside_the_arc_is_the_colour_behind_the_widget_and_inside_is_the_fill():
    tl, tr, bl, br = ui_widgets.corner_images(8, OUTER, FILL, BORDER)
    assert tl.getpixel((0, 0)) == tr.getpixel((7, 0)) == bl.getpixel((0, 7)) == br.getpixel((7, 7)) == rgb(OUTER)
    assert tl.getpixel((7, 7)) == br.getpixel((0, 0)) == rgb(FILL)


def test_the_arc_continues_the_widgets_own_straight_border():
    tl = ui_widgets.corner_images(8, OUTER, FILL, BORDER)[0]
    for edge_pixel in (tl.getpixel((7, 0)), tl.getpixel((0, 7))):  # where the corner piece meets the top / left border line
        assert dist(edge_pixel, rgb(BORDER)) < dist(edge_pixel, rgb(FILL))


def test_a_borderless_box_has_no_border_colour_in_its_corner():
    tl = ui_widgets.corner_images(6, OUTER, FILL, FILL)[0]
    assert tl.getpixel((0, 0)) == rgb(OUTER) and tl.getpixel((5, 5)) == rgb(FILL)
    assert dist(tl.getpixel((5, 0)), rgb(FILL)) < 20
