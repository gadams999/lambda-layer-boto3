#!/usr/bin/env python

# Return latest version of awwcli package
# credit to: https://stackoverflow.com/questions/4888027/python-and-pip-list-all-versions-of-a-package-thats-available/40745656#40745656

import requests
from pkg_resources import parse_version

def versions(name):
    url = "https://pypi.python.org/pypi/{}/json".format(name)
    return sorted(requests.get(url).json()["releases"], key=parse_version)

print(str(versions('awscli')[-1:][0]))
