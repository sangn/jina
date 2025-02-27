import argparse
import base64
import copy
import os
import re
import threading
import uuid
import warnings
from collections import OrderedDict, defaultdict
from contextlib import ExitStack
from typing import Optional, Union, Tuple, List, Set, Dict, overload

from .builder import build_required, _build_flow, _hanging_pods
from .. import __default_host__
from ..clients import Client, WebSocketClient
from ..enums import FlowBuildLevel, PodRoleType, FlowInspectType
from ..excepts import FlowTopologyError, FlowMissingPodError
from ..helper import (
    colored,
    get_public_ip,
    get_internal_ip,
    typename,
    ArgNamespace,
    download_mermaid_url,
)
from ..jaml import JAMLCompatible
from ..logging.logger import JinaLogger
from ..parsers import set_client_cli_parser, set_gateway_parser, set_pod_parser

__all__ = ['BaseFlow']

from ..peapods import Pod
from ..peapods.pods.compound import CompoundPod
from ..peapods.pods.factory import PodFactory


class FlowType(type(ExitStack), type(JAMLCompatible)):
    """Type of Flow, metaclass of :class:`BaseFlow`"""

    pass


_regex_port = r'(.*?):([0-9]{1,4}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])$'

if False:
    from ..peapods import BasePod


class BaseFlow(JAMLCompatible, ExitStack, metaclass=FlowType):
    """An abstract Flow object in Jina.

    .. note::

        :class:`BaseFlow` does not provide `train`, `index`, `search` interfaces.
        Please use :class:`Flow` or :class:`AsyncFlow`.

    Explanation on ``optimize_level``:

    As an example, the following Flow will generate 6 Peas,

    .. highlight:: python
    .. code-block:: python

        f = Flow.add(uses='forward', parallel=3)

    :param kwargs: other keyword arguments that will be shared by all Pods in this Flow
    :param args: Namespace args
    :param env: environment variables shared by all Pods
    """

    _cls_client = Client  #: the type of the Client, can be changed to other class

    # overload_inject_start_flow
    @overload
    def __init__(
        self,
        description: Optional[str] = None,
        inspect: Optional[str] = 'COLLECT',
        log_config: Optional[str] = None,
        name: Optional[str] = None,
        quiet: Optional[bool] = False,
        quiet_error: Optional[bool] = False,
        uses: Optional[str] = None,
        workspace: Optional[str] = './',
        **kwargs,
    ):
        """Create a Flow. Flow is how Jina streamlines and scales Executors

        :param description: The description of this object. It will be used in automatics docs UI.
        :param inspect: The strategy on those inspect pods in the flow.

          If `REMOVE` is given then all inspect pods are removed when building the flow.
        :param log_config: The YAML config of the logger used in this object.
        :param name: The name of this object.

          This will be used in the following places:
          - how you refer to this object in Python/YAML/CLI
          - visualization
          - log message header
          - automatics docs UI
          - ...

          When not given, then the default naming strategy will apply.
        :param quiet: If set, then no log will be emitted from this object.
        :param quiet_error: If set, then exception stack information will not be added to the log
        :param uses: The YAML file represents a flow
        :param workspace: The working directory for any IO operations in this object. If not set, then derive from its parent `workspace`.

        .. # noqa: DAR202
        .. # noqa: DAR101
        .. # noqa: DAR003
        """

    # overload_inject_end_flow
    def __init__(
        self,
        args: Optional['argparse.Namespace'] = None,
        env: Optional[Dict] = None,
        **kwargs,
    ):
        super().__init__()
        self._version = '1'  #: YAML version number, this will be later overridden if YAML config says the other way
        self._pod_nodes = OrderedDict()  # type: Dict[str, BasePod]
        self._inspect_pods = {}  # type: Dict[str, str]
        self._build_level = FlowBuildLevel.EMPTY
        self._last_changed_pod = [
            'gateway'
        ]  #: default first pod is gateway, will add when build()
        self._update_args(args, **kwargs)
        self._env = env
        if isinstance(self.args, argparse.Namespace):
            self.logger = JinaLogger(self.__class__.__name__, **vars(self.args))
        else:
            self.logger = JinaLogger(self.__class__.__name__)

    def _update_args(self, args, **kwargs):
        from ..parsers.flow import set_flow_parser
        from ..helper import ArgNamespace

        _flow_parser = set_flow_parser()
        if args is None:
            args = ArgNamespace.kwargs2namespace(kwargs, _flow_parser)
        self.args = args
        self._common_kwargs = kwargs
        self._kwargs = ArgNamespace.get_non_defaults_args(
            args, _flow_parser
        )  #: for yaml dump

    @staticmethod
    def _parse_endpoints(op_flow, pod_name, endpoint, connect_to_last_pod=False) -> Set:
        # parsing needs
        if isinstance(endpoint, str):
            endpoint = [endpoint]
        elif not endpoint:
            if op_flow._last_changed_pod and connect_to_last_pod:
                endpoint = [op_flow.last_pod]
            else:
                endpoint = []

        if isinstance(endpoint, (list, tuple)):
            for idx, s in enumerate(endpoint):
                if s == pod_name:
                    raise FlowTopologyError(
                        'the income/output of a pod can not be itself'
                    )
        else:
            raise ValueError(f'endpoint={endpoint} is not parsable')

        # if an endpoint is being inspected, then replace it with inspected Pod
        endpoint = set(op_flow._inspect_pods.get(ep, ep) for ep in endpoint)
        return endpoint

    @property
    def last_pod(self):
        """Last pod


        .. # noqa: DAR401


        .. # noqa: DAR201
        """
        return self._last_changed_pod[-1]

    @last_pod.setter
    def last_pod(self, name: str):
        """
        Set a Pod as the last Pod in the Flow, useful when modifying the Flow.


        .. # noqa: DAR401
        :param name: the name of the existing Pod
        """
        if name not in self._pod_nodes:
            raise FlowMissingPodError(f'{name} can not be found in this Flow')

        if self._last_changed_pod and name == self.last_pod:
            pass
        else:
            self._last_changed_pod.append(name)

        # graph is now changed so we need to
        # reset the build level to the lowest
        self._build_level = FlowBuildLevel.EMPTY

    def _add_gateway(self, needs, **kwargs):
        pod_name = 'gateway'

        kwargs.update(
            dict(
                name=pod_name,
                ctrl_with_ipc=True,  # otherwise ctrl port would be conflicted
                runtime_cls='GRPCRuntime'
                if self._cls_client == Client
                else 'RESTRuntime',
                pod_role=PodRoleType.GATEWAY,
                identity=self.args.identity,
            )
        )

        kwargs.update(self._common_kwargs)
        args = ArgNamespace.kwargs2namespace(kwargs, set_gateway_parser())

        self._pod_nodes[pod_name] = Pod(args, needs)

    def needs(
        self, needs: Union[Tuple[str], List[str]], name: str = 'joiner', *args, **kwargs
    ) -> 'BaseFlow':
        """
        Add a blocker to the Flow, wait until all peas defined in **needs** completed.


        .. # noqa: DAR401
        :param needs: list of service names to wait
        :param name: the name of this joiner, by default is ``joiner``
        :param args: additional positional arguments forwarded to the add function
        :param kwargs: additional key value arguments forwarded to the add function
        :return: the modified Flow
        """
        if len(needs) <= 1:
            raise FlowTopologyError(
                'no need to wait for a single service, need len(needs) > 1'
            )
        return self.add(
            name=name, needs=needs, pod_role=PodRoleType.JOIN, *args, **kwargs
        )

    def needs_all(self, name: str = 'joiner', *args, **kwargs) -> 'BaseFlow':
        """
        Collect all hanging Pods so far and add a blocker to the Flow; wait until all handing peas completed.

        :param name: the name of this joiner (default is ``joiner``)
        :param args: additional positional arguments which are forwarded to the add and needs function
        :param kwargs: additional key value arguments which are forwarded to the add and needs function
        :return: the modified Flow
        """
        needs = _hanging_pods(self)
        if len(needs) == 1:
            return self.add(name=name, needs=needs, *args, **kwargs)

        return self.needs(name=name, needs=needs, *args, **kwargs)

    # overload_inject_start_pod
    @overload
    def add(
        self,
        ctrl_with_ipc: Optional[bool] = False,
        daemon: Optional[bool] = False,
        description: Optional[str] = None,
        docker_kwargs: Optional[dict] = None,
        entrypoint: Optional[str] = None,
        env: Optional[dict] = None,
        expose_public: Optional[bool] = False,
        external: Optional[bool] = False,
        host: Optional[str] = '0.0.0.0',
        host_in: Optional[str] = '0.0.0.0',
        host_out: Optional[str] = '0.0.0.0',
        log_config: Optional[str] = None,
        memory_hwm: Optional[int] = -1,
        name: Optional[str] = None,
        on_error_strategy: Optional[str] = 'IGNORE',
        parallel: Optional[int] = 1,
        peas_hosts: Optional[List[str]] = None,
        polling: Optional[str] = 'ANY',
        port_ctrl: Optional[int] = None,
        port_expose: Optional[int] = None,
        port_in: Optional[int] = None,
        port_out: Optional[int] = None,
        proxy: Optional[bool] = False,
        pull_latest: Optional[bool] = False,
        py_modules: Optional[List[str]] = None,
        quiet: Optional[bool] = False,
        quiet_error: Optional[bool] = False,
        quiet_remote_logs: Optional[bool] = False,
        replicas: Optional[int] = 1,
        runtime_backend: Optional[str] = 'PROCESS',
        runtime_cls: Optional[str] = 'ZEDRuntime',
        scheduling: Optional[str] = 'LOAD_BALANCE',
        socket_in: Optional[str] = 'PULL_BIND',
        socket_out: Optional[str] = 'PUSH_BIND',
        ssh_keyfile: Optional[str] = None,
        ssh_password: Optional[str] = None,
        ssh_server: Optional[str] = None,
        timeout_ctrl: Optional[int] = 5000,
        timeout_ready: Optional[int] = 600000,
        upload_files: Optional[List[str]] = None,
        uses: Optional[str] = 'BaseExecutor',
        uses_after: Optional[str] = None,
        uses_before: Optional[str] = None,
        uses_internal: Optional[str] = 'BaseExecutor',
        volumes: Optional[List[str]] = None,
        workspace: Optional[str] = None,
        workspace_id: Optional[str] = None,
        **kwargs,
    ) -> 'BaseFlow':
        """Add an Executor to the current Flow object.

        :param ctrl_with_ipc: If set, use ipc protocol for control socket
        :param daemon: The Pea attempts to terminate all of its Runtime child processes/threads on existing. setting it to true basically tell the Pea do not wait on the Runtime when closing
        :param description: The description of this object. It will be used in automatics docs UI.
        :param docker_kwargs: Dictionary of kwargs arguments that will be passed to Docker SDK when starting the docker '
          container.

          More details can be found in the Docker SDK docs:  https://docker-py.readthedocs.io/en/stable/
        :param entrypoint: The entrypoint command overrides the ENTRYPOINT in Docker image. when not set then the Docker image ENTRYPOINT takes effective.
        :param env: The map of environment variables that are available inside runtime
        :param expose_public: If set, expose the public IP address to remote when necessary, by default it exposesprivate IP address, which only allows accessing under the same network/subnet. Important to set this to true when the Pea will receive input connections from remote Peas
        :param external: The Pod will be considered an external Pod that has been started independently from the Flow. This Pod will not be context managed by the Flow, and is considered with `--freeze-network-settings`
        :param host: The host address of the runtime, by default it is 0.0.0.0.
        :param host_in: The host address for input, by default it is 0.0.0.0
        :param host_out: The host address for output, by default it is 0.0.0.0
        :param log_config: The YAML config of the logger used in this object.
        :param memory_hwm: The memory high watermark of this pod in Gigabytes, pod will restart when this is reached. -1 means no restriction
        :param name: The name of this object.

          This will be used in the following places:
          - how you refer to this object in Python/YAML/CLI
          - visualization
          - log message header
          - automatics docs UI
          - ...

          When not given, then the default naming strategy will apply.
        :param on_error_strategy: The skip strategy on exceptions.

          - IGNORE: Ignore it, keep running all Executors in the sequel flow
          - SKIP_HANDLE: Skip all Executors in the sequel, only `pre_hook` and `post_hook` are called
          - THROW_EARLY: Immediately throw the exception, the sequel flow will not be running at all

          Note, `IGNORE`, `SKIP_EXECUTOR` and `SKIP_HANDLE` do not guarantee the success execution in the sequel flow. If something
          is wrong in the upstream, it is hard to carry this exception and moving forward without any side-effect.
        :param parallel: The number of parallel peas in the pod running at the same time, `port_in` and `port_out` will be set to random, and routers will be added automatically when necessary
        :param peas_hosts: The hosts of the peas when parallel greater than 1.
                  Peas will be evenly distributed among the hosts. By default,
                  peas are running on host provided by the argument ``host``
        :param polling: The polling strategy of the Pod (when `parallel>1`)
          - ANY: only one (whoever is idle) Pea polls the message
          - ALL: all Peas poll the message (like a broadcast)
        :param port_ctrl: The port for controlling the runtime, default a random port between [49152, 65535]
        :param port_expose: The port of the host exposed to the public
        :param port_in: The port for input data, default a random port between [49152, 65535]
        :param port_out: The port for output data, default a random port between [49152, 65535]
        :param proxy: If set, respect the http_proxy and https_proxy environment variables. otherwise, it will unset these proxy variables before start. gRPC seems to prefer no proxy
        :param pull_latest: Pull the latest image before running
        :param py_modules: The customized python modules need to be imported before loading the executor

          Note, when importing multiple files and there is a dependency between them, then one has to write the dependencies in
          reverse order. That is, if `__init__.py` depends on `A.py`, which again depends on `B.py`, then you need to write:

          --py-modules __init__.py --py-modules B.py --py-modules A.py
        :param quiet: If set, then no log will be emitted from this object.
        :param quiet_error: If set, then exception stack information will not be added to the log
        :param quiet_remote_logs: Do not display the streaming of remote logs on local console
        :param replicas: The number of replicas in the pod, `port_in` and `port_out` will be set to random, and routers will be added automatically when necessary
        :param runtime_backend: The parallel backend of the runtime inside the Pea
        :param runtime_cls: The runtime class to run inside the Pea
        :param scheduling: The strategy of scheduling workload among Peas
        :param socket_in: The socket type for input port
        :param socket_out: The socket type for output port
        :param ssh_keyfile: This specifies a key to be used in ssh login, default None. regular default ssh keys will be used without specifying this argument.
        :param ssh_password: The ssh password to the ssh server.
        :param ssh_server: The SSH server through which the tunnel will be created, can actually be a fully specified `user@server:port` ssh url.
        :param timeout_ctrl: The timeout in milliseconds of the control request, -1 for waiting forever
        :param timeout_ready: The timeout in milliseconds of a Pea waits for the runtime to be ready, -1 for waiting forever
        :param upload_files: The files on the host to be uploaded to the remote
          workspace. This can be useful when your Pod has more
          file dependencies beyond a single YAML file, e.g.
          Python files, data files.

          Note,
          - currently only flatten structure is supported, which means if you upload `[./foo/a.py, ./foo/b.pp, ./bar/c.yml]`, then they will be put under the _same_ workspace on the remote, losing all hierarchies.
          - by default, `--uses` YAML file is always uploaded.
          - uploaded files are by default isolated across the runs. To ensure files are submitted to the same workspace across different runs, use `--workspace-id` to specify the workspace.
        :param uses: The config of the executor, it could be one of the followings:
                      * an Executor-level YAML file path (.yml, .yaml, .jaml)
                      * a docker image (must start with `docker://`)
                      * the string literal of a YAML config (must start with `!` or `jtype: `)
                      * the string literal of a JSON config

                      When use it under Python, one can use the following values additionally:
                      - a Python dict that represents the config
                      - a text file stream has `.read()` interface
        :param uses_after: The executor attached after the Peas described by --uses, typically used for receiving from all parallels, accepted type follows `--uses`
        :param uses_before: The executor attached after the Peas described by --uses, typically before sending to all parallels, accepted type follows `--uses`
        :param uses_internal: The config runs inside the Docker container.

          Syntax and function are the same as `--uses`. This is designed when `--uses="docker://..."` this config is passed to
          the Docker container.
        :param volumes: The path on the host to be mounted inside the container.

          Note,
          - If separated by `:`, then the first part will be considered as the local host path and the second part is the path in the container system.
          - If no split provided, then the basename of that directory will be mounted into container's root path, e.g. `--volumes="/user/test/my-workspace"` will be mounted into `/my-workspace` inside the container.
          - All volumes are mounted with read-write mode.
        :param workspace: The working directory for any IO operations in this object. If not set, then derive from its parent `workspace`.
        :param workspace_id: the UUID for identifying the workspace. When not given a random id will be assigned.Multiple Pea/Pod/Flow will work under the same workspace if they share the same `workspace-id`.
        :return: a (new) Flow object with modification

        .. # noqa: DAR202
        .. # noqa: DAR101
        .. # noqa: DAR003
        """

    # overload_inject_end_pod
    def add(
        self,
        needs: Optional[Union[str, Tuple[str], List[str]]] = None,
        copy_flow: bool = True,
        pod_role: 'PodRoleType' = PodRoleType.POD,
        **kwargs,
    ) -> 'BaseFlow':
        """
        Add a Pod to the current Flow object and return the new modified Flow object.
        The attribute of the Pod can be later changed with :py:meth:`set` or deleted with :py:meth:`remove`

        .. # noqa: DAR401
        :param needs: the name of the Pod(s) that this Pod receives data from.
                           One can also use 'gateway' to indicate the connection with the gateway.
        :param pod_role: the role of the Pod, used for visualization and route planning
        :param copy_flow: when set to true, then always copy the current Flow and do the modification on top of it then return, otherwise, do in-line modification
        :param kwargs: other keyword-value arguments that the Pod CLI supports
        :return: a (new) Flow object with modification
        """

        op_flow = copy.deepcopy(self) if copy_flow else self

        # pod naming logic
        pod_name = kwargs.get('name', None)

        if pod_name in op_flow._pod_nodes:
            new_name = f'{pod_name}{len(op_flow._pod_nodes)}'
            self.logger.debug(
                f'"{pod_name}" is used in this Flow already! renamed it to "{new_name}"'
            )
            pod_name = new_name

        if not pod_name:
            pod_name = f'pod{len(op_flow._pod_nodes)}'

        if not pod_name.isidentifier():
            # hyphen - can not be used in the name
            raise ValueError(
                f'name: {pod_name} is invalid, please follow the python variable name conventions'
            )

        # needs logic
        needs = op_flow._parse_endpoints(
            op_flow, pod_name, needs, connect_to_last_pod=True
        )

        # set the kwargs inherit from `Flow(kwargs1=..., kwargs2=)`
        for key, value in op_flow._common_kwargs.items():
            if key not in kwargs:
                kwargs[key] = value

        # check if host is set to remote:port
        if 'host' in kwargs:
            m = re.match(_regex_port, kwargs['host'])
            if (
                kwargs.get('host', __default_host__) != __default_host__
                and m
                and 'port_expose' not in kwargs
            ):
                kwargs['port_expose'] = m.group(2)
                kwargs['host'] = m.group(1)

        # update kwargs of this Pod
        kwargs.update(dict(name=pod_name, pod_role=pod_role, num_part=len(needs)))

        parser = set_pod_parser()
        if pod_role == PodRoleType.GATEWAY:
            parser = set_gateway_parser()

        args = ArgNamespace.kwargs2namespace(kwargs, parser)

        # pod workspace if not set then derive from flow workspace
        args.workspace = os.path.abspath(args.workspace or self.workspace)

        op_flow._pod_nodes[pod_name] = PodFactory.build_pod(args, needs)
        op_flow.last_pod = pod_name

        return op_flow

    def inspect(self, name: str = 'inspect', *args, **kwargs) -> 'BaseFlow':
        """Add an inspection on the last changed Pod in the Flow

        Internally, it adds two Pods to the Flow. But don't worry, the overhead is minimized and you
        can remove them by simply using `Flow(inspect=FlowInspectType.REMOVE)` before using the Flow.

        .. highlight:: bash
        .. code-block:: bash

            Flow -- PUB-SUB -- BasePod(_pass) -- Flow
                    |
                    -- PUB-SUB -- InspectPod (Hanging)

        In this way, :class:`InspectPod` looks like a simple ``_pass`` from outside and
        does not introduce side-effects (e.g. changing the socket type) to the original Flow.
        The original incoming and outgoing socket types are preserved.

        This function is very handy for introducing an Evaluator into the Flow.

        .. seealso::

            :meth:`gather_inspect`

        :param name: name of the Pod
        :param args: args for .add()
        :param kwargs: kwargs for .add()
        :return: the new instance of the Flow
        """
        _last_pod = self.last_pod
        op_flow = self.add(
            name=name, needs=_last_pod, pod_role=PodRoleType.INSPECT, *args, **kwargs
        )

        # now remove uses and add an auxiliary Pod
        if 'uses' in kwargs:
            kwargs.pop('uses')
        op_flow = op_flow.add(
            name=f'_aux_{name}',
            needs=_last_pod,
            pod_role=PodRoleType.INSPECT_AUX_PASS,
            *args,
            **kwargs,
        )

        # register any future connection to _last_pod by the auxiliary Pod
        op_flow._inspect_pods[_last_pod] = op_flow.last_pod

        return op_flow

    def gather_inspect(
        self,
        name: str = 'gather_inspect',
        include_last_pod: bool = True,
        *args,
        **kwargs,
    ) -> 'BaseFlow':
        """Gather all inspect Pods output into one Pod. When the Flow has no inspect Pod then the Flow itself
        is returned.

        .. note::

            If ``--no-inspect`` is **not** given, then :meth:`gather_inspect` is auto called before :meth:`build`. So
            in general you don't need to manually call :meth:`gather_inspect`.

        :param name: the name of the gather Pod
        :param include_last_pod: if to include the last modified Pod in the Flow
        :param args: args for .add()
        :param kwargs: kwargs for .add()
        :return: the modified Flow or the copy of it


        .. seealso::

            :meth:`inspect`

        """
        needs = [k for k, v in self._pod_nodes.items() if v.role == PodRoleType.INSPECT]
        if needs:
            if include_last_pod:
                needs.append(self.last_pod)
            return self.add(
                name=name,
                needs=needs,
                pod_role=PodRoleType.JOIN_INSPECT,
                *args,
                **kwargs,
            )
        else:
            # no inspect node is in the graph, return the current graph
            return self

    def build(self, copy_flow: bool = False) -> 'BaseFlow':
        """
        Build the current Flow and make it ready to use

        .. note::

            No need to manually call it since 0.0.8. When using Flow with the
            context manager, or using :meth:`start`, :meth:`build` will be invoked.

        :param copy_flow: when set to true, then always copy the current Flow and do the modification on top of it then return, otherwise, do in-line modification
        :return: the current Flow (by default)

        .. note::
            ``copy_flow=True`` is recommended if you are building the same Flow multiple times in a row. e.g.

            .. highlight:: python
            .. code-block:: python

                f = Flow()
                with f:
                    f.index()

                with f.build(copy_flow=True) as fl:
                    fl.search()


        .. # noqa: DAR401
        """

        op_flow = copy.deepcopy(self) if copy_flow else self

        if op_flow.args.inspect == FlowInspectType.COLLECT:
            op_flow.gather_inspect(copy_flow=False)

        if 'gateway' not in op_flow._pod_nodes:
            op_flow._add_gateway(needs={op_flow.last_pod})

        # construct a map with a key a start node and values an array of its end nodes
        _outgoing_map = defaultdict(list)

        # if set no_inspect then all inspect related nodes are removed
        if op_flow.args.inspect == FlowInspectType.REMOVE:
            op_flow._pod_nodes = {
                k: v for k, v in op_flow._pod_nodes.items() if not v.role.is_inspect
            }
            reverse_inspect_map = {v: k for k, v in op_flow._inspect_pods.items()}

        for end, pod in op_flow._pod_nodes.items():
            # if an endpoint is being inspected, then replace it with inspected Pod
            # but not those inspect related node
            if op_flow.args.inspect.is_keep:
                pod.needs = set(
                    ep if pod.role.is_inspect else op_flow._inspect_pods.get(ep, ep)
                    for ep in pod.needs
                )
            else:
                pod.needs = set(reverse_inspect_map.get(ep, ep) for ep in pod.needs)

            for start in pod.needs:
                if start not in op_flow._pod_nodes:
                    raise FlowMissingPodError(
                        f'{start} is not in this flow, misspelled name?'
                    )
                _outgoing_map[start].append(end)

        op_flow = _build_flow(op_flow, _outgoing_map)
        hanging_pods = _hanging_pods(op_flow)
        if hanging_pods:
            op_flow.logger.warning(
                f'{hanging_pods} are hanging in this flow with no pod receiving from them, '
                f'you may want to double check if it is intentional or some mistake'
            )
        op_flow._build_level = FlowBuildLevel.GRAPH
        op_flow._update_client()
        return op_flow

    def __call__(self, *args, **kwargs):
        """Builds the Flow
        :param args: args for build
        :param kwargs: kwargs for build
        :return: the built Flow
        """
        return self.build(*args, **kwargs)

    def __enter__(self):
        class CatchAllCleanupContextManager:
            """
            This context manager guarantees, that the :method:``__exit__`` of the
            sub context is called, even when there is an Exception in the
            :method:``__enter__``.

            :param sub_context: The context, that should be taken care of.
            """

            def __init__(self, sub_context):
                self.sub_context = sub_context

            def __enter__(self):
                pass

            def __exit__(self, exc_type, exc_val, exc_tb):
                if exc_type is not None:
                    self.sub_context.__exit__(exc_type, exc_val, exc_tb)

        with CatchAllCleanupContextManager(self):
            return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        super().__exit__(exc_type, exc_val, exc_tb)

        # unset all envs to avoid any side-effect
        if self._env:
            for k in self._env.keys():
                os.unsetenv(k)
        if 'gateway' in self._pod_nodes:
            self._pod_nodes.pop('gateway')
        self._build_level = FlowBuildLevel.EMPTY
        self.logger.success(
            f'flow is closed and all resources are released, current build level is {self._build_level}'
        )
        self.logger.close()

    def start(self):
        """Start to run all Pods in this Flow.

        Remember to close the Flow with :meth:`close`.

        Note that this method has a timeout of ``timeout_ready`` set in CLI,
        which is inherited all the way from :class:`jina.peapods.peas.BasePea`


        .. # noqa: DAR401

        :return: this instance
        """

        if self._build_level.value < FlowBuildLevel.GRAPH.value:
            self.build(copy_flow=False)

        # set env only before the Pod get started
        if self._env:
            for k, v in self._env.items():
                os.environ[k] = str(v)

        for k, v in self:
            v.args.noblock_on_start = True
            if not getattr(v.args, 'external', False):
                self.enter_context(v)

        for k, v in self:
            try:
                if not getattr(v.args, 'external', False):
                    v.wait_start_success()
            except Exception as ex:
                self.logger.error(
                    f'{k}:{v!r} can not be started due to {ex!r}, Flow is aborted'
                )
                self.close()
                raise

        self.logger.info(
            f'{self.num_pods} Pods (i.e. {self.num_peas} Peas) are running in this Flow'
        )

        self._build_level = FlowBuildLevel.RUNNING
        self._show_success_message()

        return self

    @property
    def num_pods(self) -> int:
        """Get the number of Pods in this Flow


        .. # noqa: DAR201"""
        return len(self._pod_nodes)

    @property
    def num_peas(self) -> int:
        """Get the number of peas (parallel count) in this Flow


        .. # noqa: DAR201"""
        return sum(v.num_peas for v in self._pod_nodes.values())

    def __eq__(self, other: 'BaseFlow') -> bool:
        """
        Compare the topology of a Flow with another Flow.
        Identification is defined by whether two flows share the same set of edges.

        :param other: the second Flow object
        :return: result of equality check
        """

        if self._build_level.value < FlowBuildLevel.GRAPH.value:
            a = self.build()
        else:
            a = self

        if other._build_level.value < FlowBuildLevel.GRAPH.value:
            b = other.build()
        else:
            b = other

        return a._pod_nodes == b._pod_nodes

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def client(self) -> 'Client':
        """Return a :class:`Client` object attach to this Flow.

        .. # noqa: DAR201"""
        kwargs = {}
        kwargs.update(self._common_kwargs)
        if 'port_expose' not in kwargs:
            kwargs['port_expose'] = self.port_expose
        if 'host' not in kwargs:
            kwargs['host'] = self.host

        args = ArgNamespace.kwargs2namespace(kwargs, set_client_cli_parser())

        # show progress when client is used inside the flow, for better log readability
        if 'show_progress' not in kwargs:
            args.show_progress = True

        return self._cls_client(args)

    @property
    def _mermaid_str(self):
        mermaid_graph = [
            "%%{init: {'theme': 'base', "
            "'themeVariables': { 'primaryColor': '#32C8CD', "
            "'edgeLabelBackground':'#fff', 'clusterBkg': '#FFCC66'}}}%%",
            'graph LR',
        ]

        start_repl = {}
        end_repl = {}
        for node, v in self._pod_nodes.items():
            if not v.is_singleton and v.role != PodRoleType.GATEWAY:
                if v.args.replicas == 1:
                    mermaid_graph.append(
                        f'subgraph sub_{node} ["{node} ({v.args.parallel})"]'
                    )
                else:
                    mermaid_graph.append(
                        f'subgraph sub_{node} ["{node} ({v.args.replicas})({v.args.parallel})"]'
                    )
                    if v.is_head_router:
                        head_router = node + '_HEAD'
                        end_repl[node] = (head_router, '((fa:fa-random))')
                    if v.is_tail_router:
                        tail_router = node + '_TAIL'
                        start_repl[node] = (tail_router, '((fa:fa-random))')

                for i in range(v.args.replicas):
                    if v.is_head_router:
                        head_replica_router = node + f'_{i}_HEAD'
                        if v.args.replicas == 1:
                            end_repl[node] = (head_replica_router, '((fa:fa-random))')
                    if v.is_tail_router:
                        tail_replica_router = node + f'_{i}_TAIL'
                        if v.args.replicas == 1:
                            start_repl[node] = (tail_replica_router, '((fa:fa-random))')

                    p_r = '((%s))'
                    p_e = '[[%s]]'
                    if v.args.replicas > 1:
                        mermaid_graph.append(
                            f'\t{head_router}{p_r % "head"}:::pea-->{head_replica_router}{p_e % "replica_head"}:::pea'
                        )
                        mermaid_graph.append(
                            f'\t{tail_replica_router}{p_r % "replica_tail"}:::pea-->{tail_router}{p_e % "tail"}:::pea'
                        )

                    for j in range(v.args.parallel):
                        r = node
                        if v.args.replicas > 1:
                            r += f'_{i}_{j}'
                        elif v.args.parallel > 1:
                            r += f'_{j}'

                        if v.is_head_router:
                            mermaid_graph.append(
                                f'\t{head_replica_router}{p_r % "head"}:::pea-->{r}{p_e % r}:::pea'
                            )
                        if v.is_tail_router:
                            mermaid_graph.append(
                                f'\t{r}{p_e % r}:::pea-->{tail_replica_router}{p_r % "tail"}:::pea'
                            )
                mermaid_graph.append('end')

        for node, v in self._pod_nodes.items():
            ed_str = str(v.head_args.socket_in).split('_')[0]
            for need in sorted(v.needs):
                edge_str = ''
                if need in self._pod_nodes:
                    st_str = str(self._pod_nodes[need].tail_args.socket_out).split('_')[
                        0
                    ]
                    edge_str = f'|{st_str}-{ed_str}|'

                _s = start_repl.get(need, (need, f'({need})'))
                _e = end_repl.get(node, (node, f'({node})'))
                _s_role = self._pod_nodes[need].role
                _e_role = self._pod_nodes[node].role
                line_st = '-->'

                if _s_role in {PodRoleType.INSPECT, PodRoleType.JOIN_INSPECT}:
                    _s = start_repl.get(need, (need, f'{{{{{need}}}}}'))

                if _e_role == PodRoleType.GATEWAY:
                    _e = ('gateway_END', f'({node})')
                elif _e_role in {PodRoleType.INSPECT, PodRoleType.JOIN_INSPECT}:
                    _e = end_repl.get(node, (node, f'{{{{{node}}}}}'))

                if _s_role == PodRoleType.INSPECT or _e_role == PodRoleType.INSPECT:
                    line_st = '-.->'

                mermaid_graph.append(
                    f'{_s[0]}{_s[1]}:::{str(_s_role)} {line_st} {edge_str}{_e[0]}{_e[1]}:::{str(_e_role)}'
                )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.POD)} fill:#32C8CD,stroke:#009999'
        )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.INSPECT)} fill:#ff6666,color:#fff'
        )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.JOIN_INSPECT)} fill:#ff6666,color:#fff'
        )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.GATEWAY)} fill:#6E7278,color:#fff'
        )
        mermaid_graph.append(
            f'classDef {str(PodRoleType.INSPECT_AUX_PASS)} fill:#fff,color:#000,stroke-dasharray: 5 5'
        )
        mermaid_graph.append('classDef pea fill:#009999,stroke:#1E6E73')
        return '\n'.join(mermaid_graph)

    def plot(
        self,
        output: Optional[str] = None,
        vertical_layout: bool = False,
        inline_display: bool = False,
        build: bool = True,
        copy_flow: bool = False,
    ) -> 'BaseFlow':
        """
        Visualize the Flow up to the current point
        If a file name is provided it will create a jpg image with that name,
        otherwise it will display the URL for mermaid.
        If called within IPython notebook, it will be rendered inline,
        otherwise an image will be created.

        Example,

        .. highlight:: python
        .. code-block:: python

            flow = Flow().add(name='pod_a').plot('flow.svg')

        :param output: a filename specifying the name of the image to be created,
                    the suffix svg/jpg determines the file type of the output image
        :param vertical_layout: top-down or left-right layout
        :param inline_display: show image directly inside the Jupyter Notebook
        :param build: build the Flow first before plotting, gateway connection can be better showed
        :param copy_flow: when set to true, then always copy the current Flow and
                do the modification on top of it then return, otherwise, do in-line modification
        :return: the Flow
        """

        # deepcopy causes the below error while reusing a Flow in Jupyter
        # 'Pickling an AuthenticationString object is disallowed for security reasons'
        op_flow = copy.deepcopy(self) if copy_flow else self

        if build:
            op_flow.build(False)

        mermaid_str = op_flow._mermaid_str
        if vertical_layout:
            mermaid_str = mermaid_str.replace('graph LR', 'graph TD')

        image_type = 'svg'
        if output and output.endswith('jpg'):
            image_type = 'jpg'

        url = op_flow._mermaid_to_url(mermaid_str, image_type)
        showed = False
        if inline_display:
            try:
                from IPython.display import display, Image

                display(Image(url=url))
                showed = True
            except:
                # no need to panic users
                pass

        if output:
            download_mermaid_url(url, output)
        elif not showed:
            op_flow.logger.info(f'flow visualization: {url}')

        return self

    def _ipython_display_(self):
        """Displays the object in IPython as a side effect"""
        self.plot(
            inline_display=True, build=(self._build_level != FlowBuildLevel.GRAPH)
        )

    def _mermaid_to_url(self, mermaid_str: str, img_type: str) -> str:
        """
        Render the current Flow as URL points to a SVG. It needs internet connection

        :param mermaid_str: the mermaid representation
        :param img_type: image type (svg/jpg)
        :return: the url points to a SVG
        """
        if img_type == 'jpg':
            img_type = 'img'

        encoded_str = base64.b64encode(bytes(mermaid_str, 'utf-8')).decode('utf-8')

        return f'https://mermaid.ink/{img_type}/{encoded_str}'

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def port_expose(self) -> int:
        """Return the exposed port of the gateway


        .. # noqa: DAR201"""
        return self._pod_nodes['gateway'].port_expose

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def host(self) -> str:
        """Return the local address of the gateway


        .. # noqa: DAR201"""
        return self._pod_nodes['gateway'].host

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def address_private(self) -> str:
        """Return the private IP address of the gateway for connecting from other machine in the same network


        .. # noqa: DAR201"""
        return get_internal_ip()

    @property
    @build_required(FlowBuildLevel.GRAPH)
    def address_public(self) -> str:
        """Return the public IP address of the gateway for connecting from other machine in the public network


        .. # noqa: DAR201"""
        return get_public_ip()

    def __iter__(self):
        return self._pod_nodes.items().__iter__()

    def _show_success_message(self):
        if self._pod_nodes['gateway'].args.restful:
            header = 'http://'
            protocol = 'REST'
        else:
            header = 'tcp://'
            protocol = 'gRPC'

        address_table = [
            f'\t🖥️ Local access:\t'
            + colored(
                f'{header}{self.host}:{self.port_expose}', 'cyan', attrs='underline'
            ),
            f'\t🔒 Private network:\t'
            + colored(
                f'{header}{self.address_private}:{self.port_expose}',
                'cyan',
                attrs='underline',
            ),
        ]
        if self.address_public:
            address_table.append(
                f'\t🌐 Public address:\t'
                + colored(
                    f'{header}{self.address_public}:{self.port_expose}',
                    'cyan',
                    attrs='underline',
                )
            )
        self.logger.success(
            f'🎉 Flow is ready to use, accepting {colored(protocol + " request", attrs="bold")}'
        )
        self.logger.info('\n' + '\n'.join(address_table))

    def block(self):
        """Block the process until user hits KeyboardInterrupt """
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass

    def use_grpc_gateway(self, port: Optional[int] = None):
        """Change to use gRPC gateway for Flow IO.

        You can change the gateway even in the runtime.

        :param port: the new port number to expose

        """
        self._switch_gateway('GRPCRuntime', port)

    def _switch_gateway(self, gateway: str, port: int):
        restful = gateway == 'RESTRuntime'
        client = WebSocketClient if gateway == 'RESTRuntime' else Client

        # globally register this at Flow level
        self._cls_client = client
        self._common_kwargs['restful'] = restful
        if port:
            self._common_kwargs['port_expose'] = port

        # Flow is build to graph already
        if self._build_level >= FlowBuildLevel.GRAPH:
            self['gateway'].args.restful = restful
            self['gateway'].args.runtime_cls = gateway
            if port:
                self['gateway'].args.port_expose = port

        # Flow is running already, then close the existing gateway
        if self._build_level >= FlowBuildLevel.RUNNING:
            self['gateway'].close()
            self.enter_context(self['gateway'])
            self['gateway'].wait_start_success()

    def use_rest_gateway(self, port: Optional[int] = None):
        """Change to use REST gateway for IO.

        You can change the gateway even in the runtime.

        :param port: the new port number to expose

        """
        self._switch_gateway('RESTRuntime', port)

    def __getitem__(self, item):
        if isinstance(item, str):
            return self._pod_nodes[item]
        elif isinstance(item, int):
            return list(self._pod_nodes.values())[item]
        else:
            raise TypeError(f'{typename(item)} is not supported')

    def _update_client(self):
        if self._pod_nodes['gateway'].args.restful:
            self._cls_client = WebSocketClient

    @property
    def workspace(self) -> str:
        """Return the workspace path of the flow.

        .. # noqa: DAR201"""
        return os.path.abspath(self.args.workspace or './')

    @property
    def workspace_id(self) -> Dict[str, str]:
        """Get all Pods' ``workspace_id`` values in a dict


        .. # noqa: DAR201"""
        return {
            k: p.args.workspace_id for k, p in self if hasattr(p.args, 'workspace_id')
        }

    @workspace_id.setter
    def workspace_id(self, value: str):
        """Set all Pods' ``workspace_id`` to ``value``

        :param value: a hexadecimal UUID string
        """
        uuid.UUID(value)
        for k, p in self:
            if hasattr(p.args, 'workspace_id'):
                p.args.workspace_id = value
                args = getattr(p, 'peas_args', getattr(p, 'replicas_args', None))
                if args is None:
                    raise ValueError(
                        f'could not find "peas_args" or "replicas_args" on {p}'
                    )
                values = None
                if isinstance(args, dict):
                    values = args.values()
                elif isinstance(args, list):
                    values = args
                for v in values:
                    if v and isinstance(v, argparse.Namespace):
                        v.workspace_id = value
                    if v and isinstance(v, List):
                        for i in v:
                            i.workspace_id = value

    @property
    def identity(self) -> Dict[str, str]:
        """Get all Pods' ``identity`` values in a dict


        .. # noqa: DAR201
        """
        return {k: p.args.identity for k, p in self}

    @identity.setter
    def identity(self, value: str):
        """Set all Pods' ``identity`` to ``value``

        :param value: a hexadecimal UUID string
        """
        uuid.UUID(value)
        self.args.identity = value
        # Re-initiating logger with new identity
        self.logger = JinaLogger(self.__class__.__name__, **vars(self.args))
        for _, p in self:
            p.args.identity = value

    # for backward support
    join = needs

    def rolling_update(self, pod_name: str, dump_path: Optional[str] = None):
        """
        Reload Pods sequentially - only used for compound pods.

        :param dump_path: the path from which to read the dump data
        :param pod_name: pod to update
        """
        # TODO: By design after the Flow object started, Flow shouldn't have memory access to its sub-objects anymore.
        #  All controlling should be issued via Network Request, not via memory access.
        #  In the current master, we have Flow.rolling_update() & Flow.dump() method avoid the above design.
        #  Avoiding this design make the whole system NOT cloud-native.
        warnings.warn(
            'This function is experimental and facing potential refactoring',
            FutureWarning,
        )

        compound_pod = self._pod_nodes[pod_name]
        if isinstance(compound_pod, CompoundPod):
            compound_pod.rolling_update(dump_path)
        else:
            raise ValueError(
                f'The BasePod {pod_name} is not a CompoundPod and does not support updating'
            )
