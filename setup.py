from setuptools import setup, find_packages

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="pizone",
    version="0.1.0",
    author="Penny Wood",
    author_email="pypl@ninjateaparty.com",
    description="A python interface to the iZone airconditioner controller",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Swamp-Ig/pizone",
    install_requires=['requests>=2.0'],
    packages=setuptools.find_packages(exclude=['tests', 'tests.*']),
    classifiers=(
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Operating System :: OS Independent",
    ),
)