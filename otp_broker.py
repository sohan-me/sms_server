import json
import logging
import threading
import time

import redis


logger = logging.getLogger(__name__)


class OTPBroker:
    """Publish OTPs across workers and deliver them to local WebSockets."""

    def __init__(self, redis_url, on_message, channel="orbitalcore:otp"):
        self.redis_url = (redis_url or "").strip()
        self.on_message = on_message
        self.channel = channel
        self._client = None          # publish client
        self._pubsub = None          # subscribe connection
        self._thread = None
        self._lock = threading.Lock()
        self._stop = False

    @property
    def distributed(self):
        return bool(self.redis_url)

    @property
    def subscriber_alive(self):
        if not self.distributed:
            return False
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self):
        if not self.distributed:
            return True

        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            if not self._ensure_publish_client_locked():
                return False
            if not self._connect_pubsub_locked():
                return False

            self._stop = False
            self._thread = threading.Thread(
                target=self._listen,
                name="otp-redis-subscriber",
                daemon=True,
            )
            self._thread.start()
            return True

    def _ensure_publish_client_locked(self):
        try:
            if self._client is not None:
                self._client.ping()
                return True
            self._client = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            self._client.ping()
            return True
        except redis.RedisError:
            logger.exception("Unable to connect OTP Redis publish client")
            self._client = None
            return False

    def _connect_pubsub_locked(self):
        """Pubsub must not use a short socket_timeout — listen() would die when idle
        and live WS push would stop on that worker."""
        try:
            self._close_pubsub_locked()
            pubsub_client = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=None,
            )
            self._pubsub = pubsub_client.pubsub(ignore_subscribe_messages=True)
            self._pubsub.subscribe(self.channel)
            return True
        except redis.RedisError:
            logger.exception("Unable to connect to the OTP Redis subscriber")
            self._close_pubsub_locked()
            return False

    def _close_pubsub_locked(self):
        if self._pubsub is None:
            return
        try:
            self._pubsub.close()
        except Exception:
            pass
        self._pubsub = None

    def stop(self):
        """Signal the listener thread to exit and release the Redis sockets."""
        self._stop = True
        with self._lock:
            self._close_pubsub_locked()
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def publish(self, payload):
        # Always deliver to local WS clients first (same-worker live push).
        try:
            self.on_message(payload)
        except Exception:
            logger.exception("Local OTP delivery failed")

        if not self.distributed:
            return True

        if not self.start():
            return False

        with self._lock:
            if not self._ensure_publish_client_locked():
                return False
            client = self._client

        try:
            client.publish(self.channel, json.dumps(payload))
            return True
        except redis.RedisError:
            logger.exception("Unable to publish OTP through Redis")
            with self._lock:
                self._client = None
            return False

    def _listen(self):
        """Keep listening; reconnect if the pubsub connection drops."""
        while not self._stop:
            with self._lock:
                pubsub = self._pubsub
            if pubsub is None:
                with self._lock:
                    if self._stop:
                        return
                    if not self._connect_pubsub_locked():
                        pubsub = None
                    else:
                        pubsub = self._pubsub
                if pubsub is None:
                    time.sleep(1)
                    continue
            try:
                for message in pubsub.listen():
                    if self._stop:
                        return
                    try:
                        payload = json.loads(message["data"])
                        if isinstance(payload, dict):
                            self.on_message(payload)
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Ignoring invalid OTP broker message")
            except redis.RedisError:
                logger.exception("OTP Redis subscriber interrupted — reconnecting")
                with self._lock:
                    self._close_pubsub_locked()
                time.sleep(0.5)
