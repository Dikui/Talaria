"""工作台领域路由与运行时访问入口。"""

from starlette.requests import HTTPConnection

from talaria.workbench_api.runtime import Runtime


def get_runtime(connection: HTTPConnection) -> Runtime:
    return connection.app.state.runtime
