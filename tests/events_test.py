"""Tests for events.py."""

import functools
import gc
import io
import os
import re
import signal
import socket
try:
    import ssl
except ImportError:
    ssl = None
import subprocess
import sys
import threading
import time
import errno
import zmqtulip
import unittest
import unittest.mock
from test.support import find_unused_port

try:
    from asyncio import futures
    from asyncio import events
    from asyncio import transports
    from asyncio import protocols
    from asyncio import selector_events
    from asyncio import tasks
    from asyncio import test_utils
    from asyncio import locks
except ImportError:
    from tulip import futures
    from tulip import events
    from tulip import transports
    from tulip import protocols
    from tulip import selector_events
    from tulip import tasks
    from tulip import test_utils
    from tulip import locks


class MyProto(protocols.Protocol):
    done = None

    def __init__(self, loop=None):
        self.state = 'INITIAL'
        self.nbytes = 0
        if loop is not None:
            self.done = futures.Future(loop=loop)

    def connection_made(self, transport):
        self.transport = transport
        assert self.state == 'INITIAL', self.state
        self.state = 'CONNECTED'
        transport.write(b'GET / HTTP/1.0\r\nHost: example.com\r\n\r\n')

    def data_received(self, data):
        assert self.state == 'CONNECTED', self.state
        self.nbytes += len(data)

    def eof_received(self):
        assert self.state == 'CONNECTED', self.state
        self.state = 'EOF'

    def connection_lost(self, exc):
        assert self.state in ('CONNECTED', 'EOF'), self.state
        self.state = 'CLOSED'
        if self.done:
            self.done.set_result(None)


class MyDatagramProto(protocols.DatagramProtocol):
    done = None

    def __init__(self, loop=None):
        self.state = 'INITIAL'
        self.nbytes = 0
        if loop is not None:
            self.done = futures.Future(loop=loop)

    def connection_made(self, transport):
        self.transport = transport
        assert self.state == 'INITIAL', self.state
        self.state = 'INITIALIZED'

    def datagram_received(self, data, addr):
        assert self.state == 'INITIALIZED', self.state
        self.nbytes += len(data)

    def connection_refused(self, exc):
        assert self.state == 'INITIALIZED', self.state

    def connection_lost(self, exc):
        assert self.state == 'INITIALIZED', self.state
        self.state = 'CLOSED'
        if self.done:
            self.done.set_result(None)


class MyReadPipeProto(protocols.Protocol):
    done = None

    def __init__(self, loop=None):
        self.state = ['INITIAL']
        self.nbytes = 0
        self.transport = None
        if loop is not None:
            self.done = futures.Future(loop=loop)

    def connection_made(self, transport):
        self.transport = transport
        assert self.state == ['INITIAL'], self.state
        self.state.append('CONNECTED')

    def data_received(self, data):
        assert self.state == ['INITIAL', 'CONNECTED'], self.state
        self.nbytes += len(data)

    def eof_received(self):
        assert self.state == ['INITIAL', 'CONNECTED'], self.state
        self.state.append('EOF')
        self.transport.close()

    def connection_lost(self, exc):
        assert self.state == ['INITIAL', 'CONNECTED', 'EOF'], self.state
        self.state.append('CLOSED')
        if self.done:
            self.done.set_result(None)


class MyWritePipeProto(protocols.BaseProtocol):
    done = None

    def __init__(self, loop=None):
        self.state = 'INITIAL'
        self.transport = None
        if loop is not None:
            self.done = futures.Future(loop=loop)

    def connection_made(self, transport):
        self.transport = transport
        assert self.state == 'INITIAL', self.state
        self.state = 'CONNECTED'

    def connection_lost(self, exc):
        assert self.state == 'CONNECTED', self.state
        self.state = 'CLOSED'
        if self.done:
            self.done.set_result(None)


class MySubprocessProtocol(protocols.SubprocessProtocol):

    def __init__(self, loop):
        self.state = 'INITIAL'
        self.transport = None
        self.connected = futures.Future(loop=loop)
        self.completed = futures.Future(loop=loop)
        self.disconnects = {fd: futures.Future(loop=loop) for fd in range(3)}
        self.data = {1: b'', 2: b''}
        self.returncode = None
        self.got_data = {1: locks.EventWaiter(loop=loop),
                         2: locks.EventWaiter(loop=loop)}

    def connection_made(self, transport):
        self.transport = transport
        assert self.state == 'INITIAL', self.state
        self.state = 'CONNECTED'
        self.connected.set_result(None)

    def connection_lost(self, exc):
        assert self.state == 'CONNECTED', self.state
        self.state = 'CLOSED'
        self.completed.set_result(None)

    def pipe_data_received(self, fd, data):
        assert self.state == 'CONNECTED', self.state
        self.data[fd] += data
        self.got_data[fd].set()

    def pipe_connection_lost(self, fd, exc):
        assert self.state == 'CONNECTED', self.state
        if exc:
            self.disconnects[fd].set_exception(exc)
        else:
            self.disconnects[fd].set_result(exc)

    def process_exited(self):
        assert self.state == 'CONNECTED', self.state
        self.returncode = self.transport.get_returncode()


class EventLoopTestsMixin:

    def setUp(self):
        super().setUp()
        self.loop = self.create_event_loop()
        events.set_event_loop(None)

    def tearDown(self):
        # just in case if we have transport close callbacks
        test_utils.run_briefly(self.loop)

        self.loop.close()
        gc.collect()
        super().tearDown()

    def test_run_until_complete_nesting(self):
        @tasks.coroutine
        def coro1():
            yield

        @tasks.coroutine
        def coro2():
            self.assertTrue(self.loop.is_running())
            self.loop.run_until_complete(coro1())

        self.assertRaises(
            RuntimeError, self.loop.run_until_complete, coro2())

    # Note: because of the default Windows timing granularity of
    # 15.6 msec, we use fairly long sleep times here (~100 msec).

    def test_run_until_complete(self):
        t0 = self.loop.time()
        self.loop.run_until_complete(tasks.sleep(0.1, loop=self.loop))
        t1 = self.loop.time()
        self.assertTrue(0.08 <= t1-t0 <= 0.12, t1-t0)

    def test_run_until_complete_stopped(self):
        @tasks.coroutine
        def cb():
            self.loop.stop()
            yield from tasks.sleep(0.1, loop=self.loop)
        task = cb()
        self.assertRaises(RuntimeError,
                          self.loop.run_until_complete, task)

    def test_run_until_complete_timeout(self):
        t0 = self.loop.time()
        task = tasks.async(tasks.sleep(0.2, loop=self.loop), loop=self.loop)
        self.assertRaises(futures.TimeoutError,
                          self.loop.run_until_complete,
                          task, timeout=0.1)
        t1 = self.loop.time()
        self.assertTrue(0.08 <= t1-t0 <= 0.12, t1-t0)
        self.loop.run_until_complete(task)
        t2 = self.loop.time()
        self.assertTrue(0.18 <= t2-t0 <= 0.22, t2-t0)

    def test_call_later(self):
        results = []

        def callback(arg):
            results.append(arg)
            self.loop.stop()

        self.loop.call_later(0.1, callback, 'hello world')
        t0 = time.monotonic()
        self.loop.run_forever()
        t1 = time.monotonic()
        self.assertEqual(results, ['hello world'])
        self.assertTrue(0.09 <= t1-t0 <= 0.12, t1-t0)

    def test_call_soon(self):
        results = []

        def callback(arg1, arg2):
            results.append((arg1, arg2))
            self.loop.stop()

        self.loop.call_soon(callback, 'hello', 'world')
        self.loop.run_forever()
        self.assertEqual(results, [('hello', 'world')])

    def test_call_soon_threadsafe(self):
        results = []
        lock = threading.Lock()

        def callback(arg):
            results.append(arg)
            if len(results) >= 2:
                self.loop.stop()

        def run_in_thread():
            self.loop.call_soon_threadsafe(callback, 'hello')
            lock.release()

        lock.acquire()
        t = threading.Thread(target=run_in_thread)
        t.start()

        with lock:
            self.loop.call_soon(callback, 'world')
            self.loop.run_forever()
        t.join()
        self.assertEqual(results, ['hello', 'world'])

    def test_call_soon_threadsafe_same_thread(self):
        results = []

        def callback(arg):
            results.append(arg)
            if len(results) >= 2:
                self.loop.stop()

        self.loop.call_soon_threadsafe(callback, 'hello')
        self.loop.call_soon(callback, 'world')
        self.loop.run_forever()
        self.assertEqual(results, ['hello', 'world'])

    def test_run_in_executor(self):
        def run(arg):
            return (arg, threading.get_ident())
        f2 = self.loop.run_in_executor(None, run, 'yo')
        res, thread_id = self.loop.run_until_complete(f2)
        self.assertEqual(res, 'yo')
        self.assertNotEqual(thread_id, threading.get_ident())

    def test_reader_callback(self):
        r, w = test_utils.socketpair()
        bytes_read = []

        def reader():
            try:
                data = r.recv(1024)
            except BlockingIOError:
                # Spurious readiness notifications are possible
                # at least on Linux -- see man select.
                return
            if data:
                bytes_read.append(data)
            else:
                self.assertTrue(self.loop.remove_reader(r.fileno()))
                r.close()

        self.loop.add_reader(r.fileno(), reader)
        self.loop.call_soon(w.send, b'abc')
        test_utils.run_briefly(self.loop)
        self.loop.call_soon(w.send, b'def')
        self.loop.call_soon(w.close)
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.assertEqual(b''.join(bytes_read), b'abcdef')

    def test_writer_callback(self):
        r, w = test_utils.socketpair()
        w.setblocking(False)
        self.loop.add_writer(w.fileno(), w.send, b'x'*(256*1024))
        test_utils.run_briefly(self.loop)

        def remove_writer():
            self.assertTrue(self.loop.remove_writer(w.fileno()))

        self.loop.call_soon(remove_writer)
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        w.close()
        data = r.recv(256*1024)
        r.close()
        self.assertGreaterEqual(len(data), 200)

    def test_sock_client_ops(self):
        with test_utils.run_test_server(self.loop) as httpd:
            sock = socket.socket()
            sock.setblocking(False)
            self.loop.run_until_complete(
                self.loop.sock_connect(sock, httpd.address))
            self.loop.run_until_complete(
                self.loop.sock_sendall(sock, b'GET / HTTP/1.0\r\n\r\n'))
            data = self.loop.run_until_complete(
                self.loop.sock_recv(sock, 1024))
            # consume data
            self.loop.run_until_complete(
                self.loop.sock_recv(sock, 1024))
            sock.close()

        self.assertTrue(re.match(rb'HTTP/1.0 200 OK', data), data)

    def test_sock_client_fail(self):
        # Make sure that we will get an unused port
        address = None
        try:
            s = socket.socket()
            s.bind(('127.0.0.1', 0))
            address = s.getsockname()
        finally:
            s.close()

        sock = socket.socket()
        sock.setblocking(False)
        with self.assertRaises(ConnectionRefusedError):
            self.loop.run_until_complete(
                self.loop.sock_connect(sock, address))
        sock.close()

    def test_sock_accept(self):
        listener = socket.socket()
        listener.setblocking(False)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client = socket.socket()
        client.connect(listener.getsockname())

        f = self.loop.sock_accept(listener)
        conn, addr = self.loop.run_until_complete(f)
        self.assertEqual(conn.gettimeout(), 0)
        self.assertEqual(addr, client.getsockname())
        self.assertEqual(client.getpeername(), listener.getsockname())
        client.close()
        conn.close()
        listener.close()

    @unittest.skipUnless(hasattr(signal, 'SIGKILL'), 'No SIGKILL')
    def test_add_signal_handler(self):
        caught = 0

        def my_handler():
            nonlocal caught
            caught += 1

        # Check error behavior first.
        self.assertRaises(
            TypeError, self.loop.add_signal_handler, 'boom', my_handler)
        self.assertRaises(
            TypeError, self.loop.remove_signal_handler, 'boom')
        self.assertRaises(
            ValueError, self.loop.add_signal_handler, signal.NSIG+1,
            my_handler)
        self.assertRaises(
            ValueError, self.loop.remove_signal_handler, signal.NSIG+1)
        self.assertRaises(
            ValueError, self.loop.add_signal_handler, 0, my_handler)
        self.assertRaises(
            ValueError, self.loop.remove_signal_handler, 0)
        self.assertRaises(
            ValueError, self.loop.add_signal_handler, -1, my_handler)
        self.assertRaises(
            ValueError, self.loop.remove_signal_handler, -1)
        self.assertRaises(
            RuntimeError, self.loop.add_signal_handler, signal.SIGKILL,
            my_handler)
        # Removing SIGKILL doesn't raise, since we don't call signal().
        self.assertFalse(self.loop.remove_signal_handler(signal.SIGKILL))
        # Now set a handler and handle it.
        self.loop.add_signal_handler(signal.SIGINT, my_handler)
        test_utils.run_briefly(self.loop)
        os.kill(os.getpid(), signal.SIGINT)
        test_utils.run_briefly(self.loop)
        self.assertEqual(caught, 1)
        # Removing it should restore the default handler.
        self.assertTrue(self.loop.remove_signal_handler(signal.SIGINT))
        self.assertEqual(signal.getsignal(signal.SIGINT),
                         signal.default_int_handler)
        # Removing again returns False.
        self.assertFalse(self.loop.remove_signal_handler(signal.SIGINT))

    @unittest.skipUnless(hasattr(signal, 'SIGALRM'), 'No SIGALRM')
    def test_signal_handling_while_selecting(self):
        # Test with a signal actually arriving during a select() call.
        caught = 0

        def my_handler():
            nonlocal caught
            caught += 1
            self.loop.stop()

        self.loop.add_signal_handler(signal.SIGALRM, my_handler)

        signal.setitimer(signal.ITIMER_REAL, 0.01, 0)  # Send SIGALRM once.
        self.loop.run_forever()
        self.assertEqual(caught, 1)

    @unittest.skipUnless(hasattr(signal, 'SIGALRM'), 'No SIGALRM')
    def test_signal_handling_args(self):
        some_args = (42,)
        caught = 0

        def my_handler(*args):
            nonlocal caught
            caught += 1
            self.assertEqual(args, some_args)

        self.loop.add_signal_handler(signal.SIGALRM, my_handler, *some_args)

        signal.setitimer(signal.ITIMER_REAL, 0.01, 0)  # Send SIGALRM once.
        self.loop.call_later(0.015, self.loop.stop)
        self.loop.run_forever()
        self.assertEqual(caught, 1)

    def test_create_connection(self):
        with test_utils.run_test_server(self.loop) as httpd:
            f = self.loop.create_connection(
                lambda: MyProto(loop=self.loop), *httpd.address)
            tr, pr = self.loop.run_until_complete(f)
            self.assertTrue(isinstance(tr, transports.Transport))
            self.assertTrue(isinstance(pr, protocols.Protocol))
            self.loop.run_until_complete(pr.done)
            self.assertGreater(pr.nbytes, 0)
            tr.close()

    def test_create_connection_sock(self):
        with test_utils.run_test_server(self.loop) as httpd:
            sock = None
            infos = self.loop.run_until_complete(
                self.loop.getaddrinfo(
                    *httpd.address, type=socket.SOCK_STREAM))
            for family, type, proto, cname, address in infos:
                try:
                    sock = socket.socket(family=family, type=type, proto=proto)
                    sock.setblocking(False)
                    self.loop.run_until_complete(
                        self.loop.sock_connect(sock, address))
                except:
                    pass
                else:
                    break
            else:
                assert False, 'Can not create socket.'

            f = self.loop.create_connection(
                lambda: MyProto(loop=self.loop), sock=sock)
            tr, pr = self.loop.run_until_complete(f)
            self.assertTrue(isinstance(tr, transports.Transport))
            self.assertTrue(isinstance(pr, protocols.Protocol))
            self.loop.run_until_complete(pr.done)
            self.assertGreater(pr.nbytes, 0)
            tr.close()

    @unittest.skipIf(ssl is None, 'No ssl module')
    def test_create_ssl_connection(self):
        with test_utils.run_test_server(
                self.loop, use_ssl=True) as httpd:
            f = self.loop.create_connection(
                lambda: MyProto(loop=self.loop), *httpd.address, ssl=True)
            tr, pr = self.loop.run_until_complete(f)
            self.assertTrue(isinstance(tr, transports.Transport))
            self.assertTrue(isinstance(pr, protocols.Protocol))
            self.assertTrue('ssl' in tr.__class__.__name__.lower())
            self.assertTrue(
                hasattr(tr.get_extra_info('socket'), 'getsockname'))
            self.loop.run_until_complete(pr.done)
            self.assertGreater(pr.nbytes, 0)
            tr.close()

    def test_create_connection_local_addr(self):
        with test_utils.run_test_server(self.loop) as httpd:
            port = find_unused_port()
            f = self.loop.create_connection(
                lambda: MyProto(loop=self.loop),
                *httpd.address, local_addr=(httpd.address[0], port))
            tr, pr = self.loop.run_until_complete(f)
            expected = pr.transport.get_extra_info('socket').getsockname()[1]
            self.assertEqual(port, expected)
            tr.close()

    def test_create_connection_local_addr_in_use(self):
        with test_utils.run_test_server(self.loop) as httpd:
            f = self.loop.create_connection(
                lambda: MyProto(loop=self.loop),
                *httpd.address, local_addr=httpd.address)
            with self.assertRaises(OSError) as cm:
                self.loop.run_until_complete(f)
            self.assertEqual(cm.exception.errno, errno.EADDRINUSE)
            self.assertIn(str(httpd.address), cm.exception.strerror)

    def test_start_serving(self):
        proto = None

        def factory():
            nonlocal proto
            proto = MyProto()
            return proto

        f = self.loop.start_serving(factory, '0.0.0.0', 0)
        socks = self.loop.run_until_complete(f)
        self.assertEqual(len(socks), 1)
        sock = socks[0]
        host, port = sock.getsockname()
        self.assertEqual(host, '0.0.0.0')
        client = socket.socket()
        client.connect(('127.0.0.1', port))
        client.send(b'xxx')
        test_utils.run_briefly(self.loop)
        self.assertIsInstance(proto, MyProto)
        self.assertEqual('INITIAL', proto.state)
        test_utils.run_briefly(self.loop)
        self.assertEqual('CONNECTED', proto.state)
        test_utils.run_briefly(self.loop)  # windows iocp
        self.assertEqual(3, proto.nbytes)

        # extra info is available
        self.assertIsNotNone(proto.transport.get_extra_info('socket'))
        conn = proto.transport.get_extra_info('socket')
        self.assertTrue(hasattr(conn, 'getsockname'))
        self.assertEqual(
            '127.0.0.1', proto.transport.get_extra_info('addr')[0])

        # close connection
        proto.transport.close()
        test_utils.run_briefly(self.loop)  # windows iocp

        self.assertEqual('CLOSED', proto.state)

        # the client socket must be closed after to avoid ECONNRESET upon
        # recv()/send() on the serving socket
        client.close()

        # close start_serving socks
        self.loop.stop_serving(sock)

    @unittest.skipIf(ssl is None, 'No ssl module')
    def test_start_serving_ssl(self):
        proto = None

        class ClientMyProto(MyProto):
            def connection_made(self, transport):
                self.transport = transport
                assert self.state == 'INITIAL', self.state
                self.state = 'CONNECTED'

        def factory():
            nonlocal proto
            proto = MyProto(loop=self.loop)
            return proto

        here = os.path.dirname(__file__)
        sslcontext = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        sslcontext.load_cert_chain(
            certfile=os.path.join(here, 'sample.crt'),
            keyfile=os.path.join(here, 'sample.key'))

        f = self.loop.start_serving(
            factory, '127.0.0.1', 0, ssl=sslcontext)

        sock = self.loop.run_until_complete(f)[0]
        host, port = sock.getsockname()
        self.assertEqual(host, '127.0.0.1')

        f_c = self.loop.create_connection(ClientMyProto, host, port, ssl=True)
        client, pr = self.loop.run_until_complete(f_c)

        client.write(b'xxx')
        test_utils.run_briefly(self.loop)
        self.assertIsInstance(proto, MyProto)
        test_utils.run_briefly(self.loop)
        self.assertEqual('CONNECTED', proto.state)
        self.assertEqual(3, proto.nbytes)

        # extra info is available
        self.assertIsNotNone(proto.transport.get_extra_info('socket'))
        conn = proto.transport.get_extra_info('socket')
        self.assertTrue(hasattr(conn, 'getsockname'))
        self.assertEqual(
            '127.0.0.1', proto.transport.get_extra_info('addr')[0])

        # close connection
        proto.transport.close()
        self.loop.run_until_complete(proto.done)
        self.assertEqual('CLOSED', proto.state)

        # the client socket must be closed after to avoid ECONNRESET upon
        # recv()/send() on the serving socket
        client.close()

        # stop serving
        self.loop.stop_serving(sock)

    def test_start_serving_sock(self):
        proto = futures.Future(loop=self.loop)

        class TestMyProto(MyProto):
            def connection_made(self, transport):
                super().connection_made(transport)
                proto.set_result(self)

        sock_ob = socket.socket(type=socket.SOCK_STREAM)
        sock_ob.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock_ob.bind(('0.0.0.0', 0))

        f = self.loop.start_serving(TestMyProto, sock=sock_ob)
        sock = self.loop.run_until_complete(f)[0]
        self.assertIs(sock, sock_ob)

        host, port = sock.getsockname()
        self.assertEqual(host, '0.0.0.0')
        client = socket.socket()
        client.connect(('127.0.0.1', port))
        client.send(b'xxx')
        client.close()

        self.loop.stop_serving(sock)

    def test_start_serving_addr_in_use(self):
        sock_ob = socket.socket(type=socket.SOCK_STREAM)
        sock_ob.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock_ob.bind(('0.0.0.0', 0))

        f = self.loop.start_serving(MyProto, sock=sock_ob)
        sock = self.loop.run_until_complete(f)[0]
        host, port = sock.getsockname()

        f = self.loop.start_serving(MyProto, host=host, port=port)
        with self.assertRaises(OSError) as cm:
            self.loop.run_until_complete(f)
        self.assertEqual(cm.exception.errno, errno.EADDRINUSE)

        self.loop.stop_serving(sock)

    @unittest.skipUnless(socket.has_ipv6, 'IPv6 not supported')
    def test_start_serving_dual_stack(self):
        f_proto = futures.Future(loop=self.loop)

        class TestMyProto(MyProto):
            def connection_made(self, transport):
                super().connection_made(transport)
                f_proto.set_result(self)

        try_count = 0
        while True:
            try:
                port = find_unused_port()
                f = self.loop.start_serving(TestMyProto, host=None, port=port)
                socks = self.loop.run_until_complete(f)
            except OSError as ex:
                if ex.errno == errno.EADDRINUSE:
                    try_count += 1
                    self.assertGreaterEqual(5, try_count)
                    continue
                else:
                    raise
            else:
                break
        client = socket.socket()
        client.connect(('127.0.0.1', port))
        client.send(b'xxx')
        proto = self.loop.run_until_complete(f_proto)
        proto.transport.close()
        client.close()

        f_proto = futures.Future(loop=self.loop)
        client = socket.socket(socket.AF_INET6)
        client.connect(('::1', port))
        client.send(b'xxx')
        proto = self.loop.run_until_complete(f_proto)
        proto.transport.close()
        client.close()

        for s in socks:
            self.loop.stop_serving(s)

    def test_stop_serving(self):
        f = self.loop.start_serving(MyProto, '0.0.0.0', 0)
        socks = self.loop.run_until_complete(f)
        sock = socks[0]
        host, port = sock.getsockname()

        client = socket.socket()
        client.connect(('127.0.0.1', port))
        client.send(b'xxx')
        client.close()

        self.loop.stop_serving(sock)

        client = socket.socket()
        self.assertRaises(
            ConnectionRefusedError, client.connect, ('127.0.0.1', port))
        client.close()

    def test_create_datagram_endpoint(self):
        class TestMyDatagramProto(MyDatagramProto):
            def __init__(inner_self):
                super().__init__(loop=self.loop)

            def datagram_received(self, data, addr):
                super().datagram_received(data, addr)
                self.transport.sendto(b'resp:'+data, addr)

        coro = self.loop.create_datagram_endpoint(
            TestMyDatagramProto, local_addr=('127.0.0.1', 0))
        s_transport, server = self.loop.run_until_complete(coro)
        host, port = s_transport.get_extra_info('addr')

        coro = self.loop.create_datagram_endpoint(
            lambda: MyDatagramProto(loop=self.loop),
            remote_addr=(host, port))
        transport, client = self.loop.run_until_complete(coro)

        self.assertEqual('INITIALIZED', client.state)
        transport.sendto(b'xxx')
        test_utils.run_briefly(self.loop)
        self.assertEqual(3, server.nbytes)
        test_utils.run_briefly(self.loop)

        # received
        self.assertEqual(8, client.nbytes)

        # extra info is available
        self.assertIsNotNone(transport.get_extra_info('socket'))
        conn = transport.get_extra_info('socket')
        self.assertTrue(hasattr(conn, 'getsockname'))

        # close connection
        transport.close()
        self.loop.run_until_complete(client.done)
        self.assertEqual('CLOSED', client.state)
        server.transport.close()

    def test_internal_fds(self):
        loop = self.create_event_loop()
        if not isinstance(loop, selector_events.BaseSelectorEventLoop):
            return

        self.assertEqual(1, loop._internal_fds)
        loop.close()
        self.assertEqual(0, loop._internal_fds)
        self.assertIsNone(loop._csock)
        self.assertIsNone(loop._ssock)

    @unittest.skipUnless(sys.platform != 'win32',
                         "Don't support pipes for Windows")
    def test_read_pipe(self):
        proto = None

        def factory():
            nonlocal proto
            proto = MyReadPipeProto(loop=self.loop)
            return proto

        rpipe, wpipe = os.pipe()
        pipeobj = io.open(rpipe, 'rb', 1024)

        @tasks.coroutine
        def connect():
            t, p = yield from self.loop.connect_read_pipe(factory, pipeobj)
            self.assertIs(p, proto)
            self.assertIs(t, proto.transport)
            self.assertEqual(['INITIAL', 'CONNECTED'], proto.state)
            self.assertEqual(0, proto.nbytes)

        self.loop.run_until_complete(connect())

        os.write(wpipe, b'1')
        test_utils.run_briefly(self.loop)
        self.assertEqual(1, proto.nbytes)

        os.write(wpipe, b'2345')
        test_utils.run_briefly(self.loop)
        self.assertEqual(['INITIAL', 'CONNECTED'], proto.state)
        self.assertEqual(5, proto.nbytes)

        os.close(wpipe)
        self.loop.run_until_complete(proto.done)
        self.assertEqual(
            ['INITIAL', 'CONNECTED', 'EOF', 'CLOSED'], proto.state)
        # extra info is available
        self.assertIsNotNone(proto.transport.get_extra_info('pipe'))

    @unittest.skipUnless(sys.platform != 'win32',
                         "Don't support pipes for Windows")
    def test_write_pipe(self):
        proto = None
        transport = None

        def factory():
            nonlocal proto
            proto = MyWritePipeProto(loop=self.loop)
            return proto

        rpipe, wpipe = os.pipe()
        pipeobj = io.open(wpipe, 'wb', 1024)

        @tasks.coroutine
        def connect():
            nonlocal transport
            t, p = yield from self.loop.connect_write_pipe(factory, pipeobj)
            self.assertIs(p, proto)
            self.assertIs(t, proto.transport)
            self.assertEqual('CONNECTED', proto.state)
            transport = t

        self.loop.run_until_complete(connect())

        transport.write(b'1')
        test_utils.run_briefly(self.loop)
        data = os.read(rpipe, 1024)
        self.assertEqual(b'1', data)

        transport.write(b'2345')
        test_utils.run_briefly(self.loop)
        data = os.read(rpipe, 1024)
        self.assertEqual(b'2345', data)
        self.assertEqual('CONNECTED', proto.state)

        os.close(rpipe)

        # extra info is available
        self.assertIsNotNone(proto.transport.get_extra_info('pipe'))

        # close connection
        proto.transport.close()
        self.loop.run_until_complete(proto.done)
        self.assertEqual('CLOSED', proto.state)

    @unittest.skipUnless(sys.platform != 'win32',
                         "Don't support pipes for Windows")
    def test_write_pipe_disconnect_on_close(self):
        proto = None
        transport = None

        def factory():
            nonlocal proto
            proto = MyWritePipeProto(loop=self.loop)
            return proto

        rpipe, wpipe = os.pipe()
        pipeobj = io.open(wpipe, 'wb', 1024)

        @tasks.coroutine
        def connect():
            nonlocal transport
            t, p = yield from self.loop.connect_write_pipe(factory,
                                                           pipeobj)
            self.assertIs(p, proto)
            self.assertIs(t, proto.transport)
            self.assertEqual('CONNECTED', proto.state)
            transport = t

        self.loop.run_until_complete(connect())
        self.assertEqual('CONNECTED', proto.state)

        transport.write(b'1')
        test_utils.run_briefly(self.loop)
        data = os.read(rpipe, 1024)
        self.assertEqual(b'1', data)

        os.close(rpipe)

        self.loop.run_until_complete(proto.done)
        self.assertEqual('CLOSED', proto.state)

    def test_prompt_cancellation(self):
        r, w = test_utils.socketpair()
        r.setblocking(False)
        f = self.loop.sock_recv(r, 1)
        ov = getattr(f, 'ov', None)
        self.assertTrue(ov is None or ov.pending)

        def main():
            try:
                self.loop.call_soon(f.cancel)
                yield from f
            except futures.CancelledError:
                res = 'cancelled'
            else:
                res = None
            finally:
                self.loop.stop()
            return res

        start = time.monotonic()
        t = tasks.Task(main(), timeout=1, loop=self.loop)
        self.loop.run_forever()
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.1)
        self.assertEqual(t.result(), 'cancelled')
        self.assertRaises(futures.CancelledError, f.result)
        self.assertTrue(ov is None or not ov.pending)
        self.loop.stop_serving(r)

        r.close()
        w.close()

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_exec(self):
        proto = None
        transp = None

        prog = os.path.join(os.path.dirname(__file__), 'echo.py')

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_exec(
                functools.partial(MySubprocessProtocol, self.loop),
                sys.executable, prog)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.connected)
        self.assertEqual('CONNECTED', proto.state)

        stdin = transp.get_pipe_transport(0)
        stdin.write(b'Python The Winner')
        self.loop.run_until_complete(proto.got_data[1].wait(1))
        transp.close()
        self.loop.run_until_complete(proto.completed)
        self.assertEqual(-signal.SIGTERM, proto.returncode)
        self.assertEqual(b'Python The Winner', proto.data[1])

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_interactive(self):
        proto = None
        transp = None

        prog = os.path.join(os.path.dirname(__file__), 'echo.py')

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_exec(
                functools.partial(MySubprocessProtocol, self.loop),
                sys.executable, prog)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.connected)
        self.assertEqual('CONNECTED', proto.state)

        try:
            stdin = transp.get_pipe_transport(0)
            stdin.write(b'Python ')
            self.loop.run_until_complete(proto.got_data[1].wait(1))
            proto.got_data[1].clear()
            self.assertEqual(b'Python ', proto.data[1])

            stdin.write(b'The Winner')
            self.loop.run_until_complete(proto.got_data[1].wait(1))
            self.assertEqual(b'Python The Winner', proto.data[1])
        finally:
            transp.close()

        self.loop.run_until_complete(proto.completed)
        self.assertEqual(-signal.SIGTERM, proto.returncode)

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_shell(self):
        proto = None
        transp = None

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_shell(
                functools.partial(MySubprocessProtocol, self.loop),
                'echo "Python"')
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.connected)

        transp.get_pipe_transport(0).close()
        self.loop.run_until_complete(proto.completed)
        self.assertEqual(0, proto.returncode)
        self.assertTrue(all(f.done() for f in proto.disconnects.values()))
        self.assertEqual({1: b'Python\n', 2: b''}, proto.data)

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_exitcode(self):
        proto = None

        @tasks.coroutine
        def connect():
            nonlocal proto
            transp, proto = yield from self.loop.subprocess_shell(
                functools.partial(MySubprocessProtocol, self.loop),
                'exit 7', stdin=None, stdout=None, stderr=None)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.completed)
        self.assertEqual(7, proto.returncode)

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_close_after_finish(self):
        proto = None
        transp = None

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_shell(
                functools.partial(MySubprocessProtocol, self.loop),
                'exit 7', stdin=None, stdout=None, stderr=None)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.assertIsNone(transp.get_pipe_transport(0))
        self.assertIsNone(transp.get_pipe_transport(1))
        self.assertIsNone(transp.get_pipe_transport(2))
        self.loop.run_until_complete(proto.completed)
        self.assertEqual(7, proto.returncode)
        self.assertIsNone(transp.close())

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_kill(self):
        proto = None
        transp = None

        prog = os.path.join(os.path.dirname(__file__), 'echo.py')

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_exec(
                functools.partial(MySubprocessProtocol, self.loop),
                sys.executable, prog)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.connected)

        transp.kill()
        self.loop.run_until_complete(proto.completed)
        self.assertEqual(-signal.SIGKILL, proto.returncode)

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_send_signal(self):
        proto = None
        transp = None

        prog = os.path.join(os.path.dirname(__file__), 'echo.py')

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_exec(
                functools.partial(MySubprocessProtocol, self.loop),
                sys.executable, prog)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.connected)

        transp.send_signal(signal.SIGHUP)
        self.loop.run_until_complete(proto.completed)
        self.assertEqual(-signal.SIGHUP, proto.returncode)

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_stderr(self):
        proto = None
        transp = None

        prog = os.path.join(os.path.dirname(__file__), 'echo2.py')

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_exec(
                functools.partial(MySubprocessProtocol, self.loop),
                sys.executable, prog)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.connected)

        stdin = transp.get_pipe_transport(0)
        stdin.write(b'test')

        self.loop.run_until_complete(proto.completed)

        transp.close()
        self.assertEqual(b'OUT:test', proto.data[1])
        self.assertTrue(proto.data[2].startswith(b'ERR:test'), proto.data[2])
        self.assertEqual(0, proto.returncode)

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_stderr_redirect_to_stdout(self):
        proto = None
        transp = None

        prog = os.path.join(os.path.dirname(__file__), 'echo2.py')

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_exec(
                functools.partial(MySubprocessProtocol, self.loop),
                sys.executable, prog, stderr=subprocess.STDOUT)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.connected)

        stdin = transp.get_pipe_transport(0)
        self.assertIsNotNone(transp.get_pipe_transport(1))
        self.assertIsNone(transp.get_pipe_transport(2))

        stdin.write(b'test')
        self.loop.run_until_complete(proto.completed)
        self.assertTrue(proto.data[1].startswith(b'OUT:testERR:test'),
                        proto.data[1])
        self.assertEqual(b'', proto.data[2])

        transp.close()
        self.assertEqual(0, proto.returncode)

    @unittest.skipIf(sys.platform == 'win32',
                     "Don't support subprocess for Windows yet")
    def test_subprocess_close_client_stream(self):
        proto = None
        transp = None

        prog = os.path.join(os.path.dirname(__file__), 'echo3.py')

        @tasks.coroutine
        def connect():
            nonlocal proto, transp
            transp, proto = yield from self.loop.subprocess_exec(
                functools.partial(MySubprocessProtocol, self.loop),
                sys.executable, prog)
            self.assertIsInstance(proto, MySubprocessProtocol)

        self.loop.run_until_complete(connect())
        self.loop.run_until_complete(proto.connected)

        stdin = transp.get_pipe_transport(0)
        stdout = transp.get_pipe_transport(1)
        stdin.write(b'test')
        self.loop.run_until_complete(proto.got_data[1].wait(1))
        self.assertEqual(b'OUT:test', proto.data[1])

        stdout.close()
        self.loop.run_until_complete(proto.disconnects[1])
        stdin.write(b'xxx')
        self.loop.run_until_complete(proto.got_data[2].wait(1))
        self.assertEqual(b'ERR:BrokenPipeError', proto.data[2])

        transp.close()
        self.loop.run_until_complete(proto.completed)
        self.assertEqual(-signal.SIGTERM, proto.returncode)


class ZmqSelectorEventLoopTests(EventLoopTestsMixin, unittest.TestCase):

    def create_event_loop(self):
        return zmqtulip.new_event_loop()
