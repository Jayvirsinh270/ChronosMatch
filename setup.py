from setuptools import setup, Extension
from Cython.Build import cythonize
import os

include_dirs = [
    os.path.abspath("include"),
    os.path.abspath("fluxcore"),
    os.path.abspath("fluxcore/engine"),
]

extensions = [
    Extension(
        name="fluxcore.test_cy",
        sources=["fluxcore/test_cy.pyx"],
        include_dirs=include_dirs,
    ),
    Extension(
        name="fluxcore.engine.arena_cy",
        sources=[
            "fluxcore/engine/arena_cy.pyx",
            "fluxcore/engine/arena.c",
        ],
        include_dirs=include_dirs,
        extra_compile_args=["/O2"] if os.name == "nt" else ["-O3"],
    ),
    Extension(
        name="fluxcore.engine.price_level_cy",
        sources=["fluxcore/engine/price_level_cy.pyx"],
        include_dirs=include_dirs,
        extra_compile_args=["/O2"] if os.name == "nt" else ["-O3"],
    ),
    Extension(
        name="fluxcore.engine.order_book_cy",
        sources=[
            "fluxcore/engine/order_book_cy.pyx",
            "fluxcore/engine/order_book.c",
            "fluxcore/engine/arena.c",
            "fluxcore/engine/order_map.c",
        ],
        include_dirs=include_dirs,
        extra_compile_args=["/O2"] if os.name == "nt" else ["-O3"],
    ),
]

setup(
    name="fluxcore",
    packages=["fluxcore"],
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        }
    ),
)
