# -*- coding: utf-8 -*-

import signal
import os
# linux 默认就是epoll
from twisted.internet import reactor
import setproctitle

from netkit.box import Box
from connection.client_tcp_connection import ClientTCPConnectionFactory
from connection.client_udp_connection import ClientConnection as ClientUDPConnection
from connection.worker_connection import WorkerConnectionFactory
from connection.admin_connection import AdminConnectionFactory
from connection.master_connection import MasterConnectionFactory
from task_dispatcher import TaskDispatcher
from stat_counter import StatCounter
from ..share import constants
from ..share.log import logger


class Proxy(object):
    """
    proxy相关
    """

    type = constants.PROC_TYPE_PROXY

    client_tcp_connection_factory_class = ClientTCPConnectionFactory
    client_udp_connection_class = ClientUDPConnection
    worker_connection_factory_class = WorkerConnectionFactory
    admin_connection_factory_class = AdminConnectionFactory
    master_connection_factory_class = MasterConnectionFactory

    app = None

    # 任务调度器
    task_dispatcher = None
    # 统计
    stat_counter = None

    # master的连接，因为一定只有一个，所以就一个变量即可
    master_conn = None

    def __init__(self, app):
        """
        构造函数
        :return:
        """
        self.app = app

        self.task_dispatcher = TaskDispatcher(self, self._on_workers_reload_over)
        self.stat_counter = StatCounter(self.app.config['TASKS_TIME_BENCHMARK'])

    def run(self):
        setproctitle.setproctitle(self.app.make_proc_name(self.type))

        # 主进程
        self._handle_proc_signals()

        ipc_directory = self.app.config['IPC_ADDRESS_DIRECTORY']
        if not os.path.exists(ipc_directory):
            os.makedirs(ipc_directory)

        # 启动监听master
        master_address = os.path.join(ipc_directory, self.app.config['MASTER_ADDRESS'])
        if os.path.exists(master_address):
            os.remove(master_address)
        reactor.listenUNIX(master_address, self.master_connection_factory_class(self))

        # 启动监听worker
        for group_id in self.app.config['GROUP_CONFIG']:
            ipc_address = os.path.join(ipc_directory, self.app.config['WORKER_ADDRESS_TPL'] % group_id)

            # 防止之前异常导致遗留
            if os.path.exists(ipc_address):
                os.remove(ipc_address)

            # 给内部worker通信用的
            reactor.listenUNIX(ipc_address, self.worker_connection_factory_class(self, group_id))

        # 启动对外监听
        if self.app.config['TCP']:
            reactor.listenTCP(self.app.config['PORT'], self.client_tcp_connection_factory_class(self),
                              backlog=self.app.config['PROXY_BACKLOG'], interface=self.app.config['HOST'])

        if self.app.config['UDP']:
            reactor.listenUDP(
                self.app.config['PORT'], self.client_udp_connection_class(self),
                interface=self.app.config['HOST']
            )

        # 启动admin服务
        admin_address = self.app.config['ADMIN_ADDRESS']

        if admin_address:
            if isinstance(admin_address, (list, tuple)):
                # 说明是网络协议
                reactor.listenTCP(admin_address[1], self.admin_connection_factory_class(self),
                                  interface=admin_address[0])
            elif isinstance(admin_address, str):
                # 说明是文件
                admin_address = os.path.join(ipc_directory, admin_address)
                # 防止之前异常导致遗留
                if os.path.exists(admin_address):
                    os.remove(admin_address)
                reactor.listenUNIX(admin_address, self.admin_connection_factory_class(self))
            else:
                logger.error('invalid admin address. proxy: %s, admin_address: %s', self, admin_address)

        try:
            reactor.run(installSignalHandlers=False)
        except KeyboardInterrupt:
            pass
        except:
            logger.error('exc occur. proxy: %s', self, exc_info=True)

    def _handle_proc_signals(self):
        def stop_handler(signum, frame):
            """
            在centos6下，callFromThread(stop)无效，因为处理不够及时
            """
            try:
                reactor.stop()
            except:
                pass

        def safe_reload_handler(signum, frame):
            """
            让所有子进程重新加载
            """
            self.task_dispatcher.start_reload()

        # 强制结束，抛出异常终止程序进行
        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGQUIT, stop_handler)
        # 直接停止
        signal.signal(signal.SIGTERM, stop_handler)
        # 忽略，因为这个时候是在重启worker
        signal.signal(signal.SIGHUP, safe_reload_handler)

    def _on_workers_reload_over(self):
        """
        当workers reload之后的操作
        需要给master通知，让master替换掉workers
        :return:
        """
        if self.master_conn and self.master_conn.transport:
            box = Box(dict(
                cmd=constants.CMD_MASTER_REPLACE_WORKERS
            ))
            self.master_conn.transport.write(box.pack())

    def __repr__(self):
        return '<%s name: %s>' % (
            type(self).__name__, self.app.name
        )
