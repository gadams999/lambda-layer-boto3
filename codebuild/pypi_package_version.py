#!/usr/bin/env python

# Return latest version of awwcli package
# credit to: https://stackoverflow.com/questions/4888027/python-and-pip-list-all-versions-of-a-package-thats-available/40745656#40745656

# returns the latest version of the submitted package name

import requests
import sys
from pkg_resources import parse_version

def versions(name):
    url = "https://pypi.python.org/pypi/{}/json".format(name)
    return sorted(requests.get(url).json()["releases"], key=parse_version)

try:
    package_name = str(sys.argv[1])
    print(str(versions(package_name)[-1:][0]))
except Exception as e:
    exit(1)
