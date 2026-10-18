from setuptools import setup, Extension
from Cython.Build import cythonize
import os

include_dirs = [
    os.path.abspath("include"),
    os.path.abspath("chronos_match"),
    os.path.abspath("chronos_match/engine"),
]

extensions = [
    Extension(
        name="chronos_match.test_cy",
        sources=["chronos_match/test_cy.pyx"],
        include_dirs=include_dirs,
    ),
    Extension(
        name="chronos_match.engine.arena_cy",
        sources=[
            "chronos_match/engine/arena_cy.pyx",
            "chronos_match/engine/arena.c",
        ],
        include_dirs=include_dirs,
        extra_compile_args=["/O2"] if os.name == "nt" else ["-O3"],
    ),
    Extension(
        name="chronos_match.engine.price_level_cy",
        sources=["chronos_match/engine/price_level_cy.pyx"],
        include_dirs=include_dirs,
        extra_compile_args=["/O2"] if os.name == "nt" else ["-O3"],
    ),
    Extension(
        name="chronos_match.engine.order_book_cy",
        sources=[
            "chronos_match/engine/order_book_cy.pyx",
            "chronos_match/engine/order_book.c",
            "chronos_match/engine/arena.c",
            "chronos_match/engine/order_map.c",
        ],
        include_dirs=include_dirs,
        extra_compile_args=["/O2"] if os.name == "nt" else ["-O3"],
    ),
]

setup(
    name="chronos_match",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        }
    ),
)
