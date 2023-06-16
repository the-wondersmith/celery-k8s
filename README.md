# Celery K8s
### Run Celery [`Task`s](https://docs.celeryq.dev/en/stable/userguide/tasks.html) as Kubernetes [`Job`s](https://kubernetes.io/docs/concepts/workloads/controllers/job/)

![python](https://img.shields.io/pypi/pyversions/celery-k8s.svg)
![version](https://img.shields.io/pypi/v/celery-k8s.svg)
![downloads](https://img.shields.io/pypi/dm/celery-k8s.svg)
![format](https://img.shields.io/pypi/format/celery-k8s.svg)

<p align="center" width="100%">
    <img width="55%" src="https://raw.githubusercontent.com/the-wondersmith/celery-k8s/main/icon.svg">
</p>

> Free software: GNU Affero General Public License v3+

## Getting Started

### Installation

#### Using Poetry _(preferred)_

```
poetry add celery-k8s
```

#### Using `pip` & [PyPI.org](https://pypi.org/project/celery-k8s/)

```
pip install celery-k8s
```

#### Using `pip` & [GitHub](https://github.com/the-wondersmith/celery-k8s.git)

```
pip install git+https://github.com/the-wondersmith/celery-k8s.git
```

### Using `pip` & A Local Copy Of The Repo

```
git clone https://github.com/the-wondersmith/celery-k8s.git
cd celery-k8s
pip install -e "$(pwd)"
```


### Configure Celery

- Import `celery_k8s` in the same module where your Celery "app" is defined
- Ensure that `celery_k8s` is imported **_before_** any other
  Celery code is actually called (thus ensuring that the `K8sTransport` class
  is able to properly register itself with `kombu`)

```python
"""My super awesome Celery app."""

# ...
from celery import Celery

# add the following import
import celery_k8s  # noqa: F401

# ensure the import is called *before*
# your Celery app is defined

app = Celery(
    "my-super-awesome-celery-app",
    # use 'k8s://' as your broker
    # url, everything after the
    # double slashes is ignored
    broker="k8s://celery-namespace",
)
```

## Developing / Testing / Contributing

> **NOTE:** _Our preferred packaging and dependency manager is [Poetry](https://python-poetry.org/)._
>           _Installation instructions can be found [here](https://python-poetry.org/docs/#installing-with-the-official-installer)._

### Developing

Clone the repo and install the dependencies
```bash
$ git clone https://github.com/the-wondersmith/celery-k8s.git \
  && cd celery-k8s \
  && poetry install --sync
```

Optionally, if you do not have or prefer _not_ to use Poetry, `celery-k8s` is
fully PEP-517 compliant and can be installed directly by any PEP-517-compliant package
manager.

```bash
$ cd celery-k8s \
  && pip install -e "$(pwd)"
```

> **TODO:** _Coming Soon™_

### Testing

To run the test suite:

```bash
$ poetry run pytest tests/
```

### Contributing

> **TODO:** _Coming Soon™_
