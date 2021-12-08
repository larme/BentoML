import typing as t
from typing import TYPE_CHECKING

from simple_di import inject
from simple_di import Provide

from ..types import Tag
from ..types import PathType
from ..utils import LazyLoader
from ..models import Model
from ..models import PKL_EXT
from ..models import SAVE_NAMESPACE
from ..runner import Runner
from ...exceptions import BentoMLException
from ...exceptions import MissingDependencyException
from ..configuration.containers import BentoMLContainer

if TYPE_CHECKING:
    import numpy as np
    from sklearn.base import BaseEstimator
    from sklearn.pipeline import Pipeline
    from pandas.core.frame import DataFrame

    from ..models import ModelStore

try:
    import joblib
    from joblib import parallel_backend
except ImportError:  # pragma: no cover
    try:
        from sklearn.utils._joblib import joblib
        from sklearn.utils._joblib import parallel_backend
    except ImportError:
        raise MissingDependencyException(
            """sklearn is required in order to use the module `bentoml.sklearn`, install
             sklearn with `pip install sklearn`. For more information, refer to
             https://scikit-learn.org/stable/install.html
            """
        )

try:
    import importlib.metadata as importlib_metadata
except ImportError:
    import importlib_metadata

_sklearn_version = importlib_metadata.version("scikit-learn")

np = LazyLoader("np", globals(), "numpy")  # noqa: F811
pd = LazyLoader("pd", globals(), "pandas")


def _get_model_info(
    tag: t.Union[str, Tag], model_store: "ModelStore"
) -> t.Tuple["Model", PathType]:
    model = model_store.get(tag)
    if model.info.module != __name__:
        raise BentoMLException(  # pragma: no cover
            f"Model {tag} was saved with module {model.info.module}, failed loading"
            f" with {__name__}."
        )
    model_file = model.path_of(f"{SAVE_NAMESPACE}{PKL_EXT}")

    return model, model_file


@inject
def load(
    tag: t.Union[str, Tag],
    model_store: "ModelStore" = Provide[BentoMLContainer.model_store],
) -> t.Union["BaseEstimator", "Pipeline"]:
    """
    Load a model from BentoML local modelstore with given name.

    Args:
        tag (`str`):
            Tag of a saved model in BentoML local modelstore.
        model_store (`~bentoml._internal.models.store.ModelStore`, default to `BentoMLContainer.model_store`):
            BentoML modelstore, provided by DI Container.

    Returns:
        an instance of sklearn model from BentoML modelstore.

    Examples:
        import bentoml.sklearn
        sklearn = bentoml.sklearn.load('my_model:20201012_DE43A2')

    """  # noqa
    _, model_file = _get_model_info(tag, model_store)
    return joblib.load(model_file)


@inject
def save(
    name: str,
    model: t.Union["BaseEstimator", "Pipeline"],
    *,
    metadata: t.Optional[t.Dict[str, t.Any]] = None,
    model_store: "ModelStore" = Provide[BentoMLContainer.model_store],
) -> Tag:
    """
    Save a model instance to BentoML modelstore.

    Args:
        name (`str`):
            Name for given model instance. This should pass Python identifier check.
        model (``):
            Instance of model to be saved
        metadata (`t.Optional[t.Dict[str, t.Any]]`, default to `None`):
            Custom metadata for given model.
        model_store (`~bentoml._internal.models.store.ModelStore`, default to `BentoMLContainer.model_store`):
            BentoML modelstore, provided by DI Container.

    Returns:
        tag (`str` with a format `name:version`) where `name` is the defined name user
        set for their models, and version will be generated by BentoML.

    Examples:

    """  # noqa
    context = {
        "framework": "sklearn",
        "sklearn_version": _sklearn_version
    }

    _model = Model.create(
        name,
        module=__name__,
        metadata=metadata,
        context=context,
    )

    joblib.dump(model, _model.path_of(f"{SAVE_NAMESPACE}{PKL_EXT}"))

    _model.save(model_store)
    return _model.tag


class _SklearnRunner(Runner):
    @inject
    def __init__(
        self,
        tag: t.Union[str, Tag],
        function_name: str,
        resource_quota: t.Optional[t.Dict[str, t.Any]],
        batch_options: t.Optional[t.Dict[str, t.Any]],
        model_store: "ModelStore" = Provide[BentoMLContainer.model_store],
    ):
        super().__init__(f"{tag}-{function_name}", resource_quota, batch_options)
        model_info, model_file = _get_model_info(tag, model_store)
        self._model_store = model_store
        self._model_info = model_info
        self._model_file = model_file
        self._backend = "loky"
        self._function_name = function_name

    @property
    def num_concurrency_per_replica(self) -> int:
        return int(round(self.resource_quota.cpu))

    @property
    def num_replica(self) -> int:
        return 1

    @property
    def required_models(self) -> t.List[Tag]:
        return [self._model_info.tag]

    # pylint: disable=attribute-defined-outside-init
    def _setup(self) -> None:
        self._model = joblib.load(filename=self._model_file)
        self._infer_func = getattr(self._model, self._function_name)

    # pylint: disable=arguments-differ
    def _run_batch(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        inputs: t.Union["np.ndarray[t.Any, np.dtype[t.Any]]", "DataFrame"],
    ) -> "np.ndarray[t.Any, np.dtype[t.Any]]":
        with parallel_backend(self._backend, n_jobs=self.num_concurrency_per_replica):
            return self._infer_func(inputs)


@inject
def load_runner(
    tag: t.Union[str, Tag],
    function_name: str = "predict",
    *,
    resource_quota: t.Union[None, t.Dict[str, t.Any]] = None,
    batch_options: t.Union[None, t.Dict[str, t.Any]] = None,
    model_store: "ModelStore" = Provide[BentoMLContainer.model_store],
) -> "_SklearnRunner":

    """
    Runner represents a unit of serving logic that can be scaled horizontally to
    maximize throughput. `bentoml.sklearn.load_runner` implements a Runner class that
    wrap around a Sklearn joblib model, which optimize it for the BentoML runtime.

    Args:
        tag (`str`):
            Model tag to retrieve model from modelstore.
        function_name (`str`, `optional`, default to `predict`):
            Predict function used by a given sklearn model.
        resource_quota (`t.Dict[str, t.Any]`, default to `None`):
            Dictionary to configure resources allocation for runner.
        batch_options (`t.Dict[str, t.Any]`, default to `None`):
            Dictionary to configure batch options for runner in a service context.
        model_store (`~bentoml._internal.models.store.ModelStore`, default to `BentoMLContainer.model_store`):
            BentoML modelstore, provided by DI Container.

    Returns:
        Runner instances for the target `bentoml.sklearn` model

    Examples::
        import bentoml
        import bentoml.sklearn
        import numpy as np

        from bentoml.io import NumpyNdarray

        input_data = NumpyNdarray()
        runner = bentoml.sklearn.load_runner("my_model:20201012_DE43A2")
        runner.run(input_data)
    """  # noqa
    return _SklearnRunner(
        tag=tag,
        function_name=function_name,
        resource_quota=resource_quota,
        batch_options=batch_options,
        model_store=model_store,
    )
