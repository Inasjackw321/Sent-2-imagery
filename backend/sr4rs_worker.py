"""SR4RS, run in its own Python: one stack in, one stack four times finer out.

This file is run by the SR4RS runtime -- a separate Python with TensorFlow in
it, which the app sets up the first time the button is pressed (see sr4rs.py).
It is not imported by the app at run time and imports nothing from the app:
only numpy, and TensorFlow inside main(). The app imports it in tests and for
tiles(), so the arithmetic below is checked without TensorFlow.

    python -I sr4rs_worker.py MODEL_DIR IN.npy OUT.npy

IN.npy is (4, rows, cols) float32 in the bands the model was trained on --
B4, B3, B2, B8: red, green, blue, near-infrared -- as Sentinel-2 digital
numbers, reflectance times 10 000. OUT.npy is (4, rows*4, cols*4), the same
units. The model scales into and out of its own range inside its graph.

Why the tiles are the shape they are. The model (Cresson, SR4RS, 2022) is
fully convolutional and exported with its output already trimmed: output_64
is the 4x picture with 64 fine pixels cut off each side, where a tile's edge
leaves the network without context. 64 fine pixels are 16 coarse ones. So a
coarse tile of 160 pixels gives a fine tile of 640 - 128 = 512, covering the
coarse tile's middle 128 pixels -- exactly the receptive and expression
fields the project's own sr.py hands OTBTF. Tiles step 128 coarse pixels, so
every fine pixel is drawn once, from a tile that had 16 coarse pixels of real
context round it. The whole picture is padded by mirroring 16 pixels first so
its own edge gets the same.
"""

from __future__ import annotations

import sys

import numpy as np

SCALE = 4
PAD_FINE = 64                      # the output_64 trim, in fine pixels
MARGIN = PAD_FINE // SCALE         # the same, in coarse pixels: 16
TILE_OUT = 512                     # fine pixels a side per tile
STEP = TILE_OUT // SCALE           # coarse pixels per tile: 128
TILE_IN = STEP + 2 * MARGIN        # what goes in: 160
INPUT = "lr_input:0"
OUTPUT = f"output_{PAD_FINE}:0"
BATCH = 4


def tiles(rows: int, cols: int, step: int = STEP) -> list[tuple[int, int]]:
    """The coarse corner of every tile, over the picture padded to whole steps."""
    down = -(-rows // step)
    across = -(-cols // step)
    return [(y * step, x * step) for y in range(down) for x in range(across)]


def padded(stack: np.ndarray, step: int = STEP, margin: int = MARGIN) -> np.ndarray:
    """The stack mirrored `margin` pixels outwards, and on to whole steps.

    Mirrored rather than repeated: a repeated edge is a smear the network has
    never seen and draws stripes from. Small pictures reflect a reflection,
    which numpy's 'symmetric' does for any size where 'reflect' would refuse.
    """
    _, rows, cols = stack.shape
    extra_r = -rows % step
    extra_c = -cols % step
    return np.pad(stack, ((0, 0), (margin, margin + extra_r), (margin, margin + extra_c)),
                  mode="symmetric")


def enhance(stack: np.ndarray, run, batch: int = BATCH) -> np.ndarray:
    """(4, rows, cols) through `run`, tile by tile, to (4, rows*4, cols*4).

    `run` takes (n, TILE_IN, TILE_IN, 4) and returns (n, TILE_OUT, TILE_OUT, 4):
    the network in TensorFlow's own channel-last layout, or a stand-in.
    """
    bands, rows, cols = stack.shape
    big = padded(stack.astype("float32"))
    corners = tiles(rows, cols)
    out = np.zeros((bands, -(-rows // STEP) * TILE_OUT, -(-cols // STEP) * TILE_OUT),
                   dtype="float32")
    for at in range(0, len(corners), batch):
        group = corners[at:at + batch]
        inputs = np.stack([big[:, y:y + TILE_IN, x:x + TILE_IN].transpose(1, 2, 0)
                           for y, x in group])
        result = np.asarray(run(inputs), dtype="float32")
        if result.shape != (len(group), TILE_OUT, TILE_OUT, bands):
            raise ValueError(f"SR4RS returned {result.shape}, not "
                             f"{(len(group), TILE_OUT, TILE_OUT, bands)}")
        for (y, x), tile in zip(group, result):
            out[:, y * SCALE:y * SCALE + TILE_OUT,
                x * SCALE:x * SCALE + TILE_OUT] = tile.transpose(2, 0, 1)
    return out[:, :rows * SCALE, :cols * SCALE]


def _tensorflow_runner(model_dir: str):
    """The SavedModel, loaded the way it was saved: a TF1 graph in a session.

    simple_save wrote it from a graph whose lr_input is a placeholder with a
    default -- the training data pipeline. Feeding it bypasses that default,
    so nothing of the pipeline runs, and only the output asked for is
    computed.
    """
    import tensorflow as tf

    tf.compat.v1.disable_eager_execution()
    graph = tf.Graph()
    session = tf.compat.v1.Session(graph=graph)
    with graph.as_default():
        tf.compat.v1.saved_model.loader.load(session, ["serve"], model_dir)
    x = graph.get_tensor_by_name(INPUT)
    y = graph.get_tensor_by_name(OUTPUT)
    dtype = x.dtype.as_numpy_dtype

    def run(batch: np.ndarray) -> np.ndarray:
        if np.issubdtype(dtype, np.integer):
            info = np.iinfo(dtype)
            batch = np.clip(np.rint(batch), info.min, info.max)
        return session.run(y, feed_dict={x: batch.astype(dtype)})

    return run


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: sr4rs_worker.py MODEL_DIR IN.npy OUT.npy", file=sys.stderr)
        return 2
    model_dir, source, target = argv[1:]
    stack = np.load(source, allow_pickle=False)
    if stack.ndim != 3 or stack.shape[0] != 4:
        print(f"expected (4, rows, cols), got {stack.shape}", file=sys.stderr)
        return 2
    out = enhance(stack, _tensorflow_runner(model_dir))
    with open(target, "wb") as f:
        np.save(f, out, allow_pickle=False)
    print(f"SR4RS {stack.shape[1]}x{stack.shape[2]} -> {out.shape[1]}x{out.shape[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
