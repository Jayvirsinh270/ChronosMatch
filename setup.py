from setuptools import setup, Extension
from Cython.Build import cythonize
import os

include_dirs = [
    os.path.abspath("include"),
    os.path.abspath("chronos_match"),
]

extensions = [
    Extension(
        name="chronos_match.test_cy",
        sources=["chronos_match/test_cy.pyx"],
        include_dirs=include_dirs,
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
