import json
import logging
import threading

import redis


logger = logging.getLogger(__name__)


class OTPBroker:
    """Publish OTPs across workers and deliver them to local WebSockets."""

    def __init__(self, redis_url, on_message, channel="orbitalcore:otp"):
        self.redis_url = (redis_url or "").strip()
        self.on_message = on_message
        self.channel = channel
        self._client = None
        self._pubsub = None
        self._thread = None
        self._lock = threading.Lock()

    @property
    def distributed(self):
        return bool(self.redis_url)

    def start(self):
        if not self.distributed:
            return True

        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            try:
                self._client = redis.Redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                )
                self._client.ping()
                self._pubsub = self._client.pubsub(
                    ignore_subscribe_messages=True
                )
                self._pubsub.subscribe(self.channel)
            except redis.RedisError:
                logger.exception("Unable to connect to the OTP Redis broker")
                self._client = None
                self._pubsub = None
                return False

            self._thread = threading.Thread(
                target=self._listen,
                name="otp-redis-subscriber",
                daemon=True,
            )
            self._thread.start()
            return True

    def publish(self, payload):
        if not self.distributed:
            self.on_message(payload)
            return True

        if not self.start():
            self.on_message(payload)
            return False

        try:
            self._client.publish(self.channel, json.dumps(payload))
            return True
        except redis.RedisError:
            logger.exception("Unable to publish OTP through Redis")
            self.on_message(payload)
            return False

    def _listen(self):
        try:
            for message in self._pubsub.listen():
                try:
                    payload = json.loads(message["data"])
                    if isinstance(payload, dict):
                        self.on_message(payload)
                except (KeyError, TypeError, ValueError):
                    logger.warning("Ignoring invalid OTP broker message")
        except redis.RedisError:
            logger.exception("OTP Redis subscriber stopped")
