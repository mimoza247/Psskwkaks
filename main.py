import asyncio
import logging
import os
import json
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors import (
    FloodWaitError, 
    UserNotParticipantError, 
    UserPrivacyRestrictedError,
    ChatAdminRequiredError,
    RPCError
)
from collections import defaultdict, deque
import signal
import sys
import aiofiles

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot_log.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)

# Данные для аутентификации - оставляем как есть по заданию
API_ID = 22273376
API_HASH = '8bd050e7e9506f984ab5339ecf56d79e'
BOT_TOKEN = '7732961270:AAGLF7DLVFHJCyx_XfD-yqq3jFXQj_1ov_Q'

# Константы для настройки бота
POLL_INTERVAL = 30  # Интервал проверки в секундах (увеличен для избежания лимитов API)
HISTORY_FILE = 'user_activity_history.json'
ALLOWED_USERS = []  # Список пользователей, имеющих доступ к боту (пусто = все пользователи)

class UserMonitoringService:
    def __init__(self, client):
        self.client = client
        self.monitored_users = {}  # user_id -> task
        self.user_activity = defaultdict(list)
        self.user_info_cache = {}  # user_id -> {username, first_name, last_name}
        self._load_activity_history()
    
    async def _load_activity_history(self):
        """Загружает историю активности из файла"""
        try:
            if os.path.exists(HISTORY_FILE):
                async with aiofiles.open(HISTORY_FILE, mode='r', encoding='utf-8') as f:
                    content = await f.read()
                    if content.strip():
                        history = json.loads(content)
                        # Преобразование строк дат обратно в объекты datetime
                        for user_id, sessions in history.items():
                            user_id = int(user_id)  # Ключи в JSON всегда строки
                            for session in sessions:
                                if session['start']:
                                    session['start'] = datetime.fromisoformat(session['start'])
                                if session['end'] and session['end'] != "В сети":
                                    session['end'] = datetime.fromisoformat(session['end'])
                            self.user_activity[user_id] = sessions
                        logger.info(f"Загружена история активности для {len(history)} пользователей")
        except Exception as e:
            logger.error(f"Ошибка при загрузке истории активности: {e}")
    
    async def _save_activity_history(self):
        """Сохраняет историю активности в файл"""
        try:
            # Преобразование datetime объектов в строки для JSON
            serializable_activity = {}
            for user_id, sessions in self.user_activity.items():
                serializable_sessions = []
                for session in sessions:
                    serializable_session = {}
                    if session['start']:
                        serializable_session['start'] = session['start'].isoformat()
                    else:
                        serializable_session['start'] = None
                    
                    if session['end']:
                        if isinstance(session['end'], datetime):
                            serializable_session['end'] = session['end'].isoformat()
                        else:
                            serializable_session['end'] = session['end']  # Для случая "В сети"
                    else:
                        serializable_session['end'] = None
                    
                    serializable_sessions.append(serializable_session)
                serializable_activity[str(user_id)] = serializable_sessions  # JSON ключи - строки
            
            async with aiofiles.open(HISTORY_FILE, mode='w', encoding='utf-8') as f:
                await f.write(json.dumps(serializable_activity, indent=2))
            logger.info(f"Сохранена история активности для {len(serializable_activity)} пользователей")
        except Exception as e:
            logger.error(f"Ошибка при сохранении истории активности: {e}")
    
    async def get_user_entity(self, user_identifier):
        """Получает entity пользователя по идентификатору"""
        try:
            # Числовой ID
            if isinstance(user_identifier, int) or user_identifier.isdigit():
                user_id = int(user_identifier)
                entity = await self.client.get_entity(user_id)
                return entity
            
            # Если указан username без @, добавляем @
            if not user_identifier.startswith('@'):
                try:
                    entity = await self.client.get_entity(user_identifier)
                    return entity
                except ValueError:
                    try:
                        entity = await self.client.get_entity(f"@{user_identifier}")
                        return entity
                    except Exception as e:
                        logger.error(f"Не удалось найти пользователя по username {user_identifier}: {e}")
                        raise ValueError(f"Пользователь не найден: {user_identifier}")
            
            # Если username с @
            entity = await self.client.get_entity(user_identifier)
            return entity
        except Exception as e:
            logger.error(f"Ошибка при получении entity пользователя {user_identifier}: {e}")
            raise ValueError(f"Не удалось найти пользователя: {user_identifier}")
    
    async def start_monitoring(self, user_identifier):
        """Начинает мониторинг пользователя"""
        try:
            entity = await self.get_user_entity(user_identifier)
            user_id = entity.id
            
            # Кэшируем информацию о пользователе
            self.user_info_cache[user_id] = {
                'username': entity.username,
                'first_name': getattr(entity, 'first_name', None),
                'last_name': getattr(entity, 'last_name', None)
            }
            
            # Проверяем, не мониторится ли уже пользователь
            if user_id in self.monitored_users and not self.monitored_users[user_id].done():
                return f"Пользователь {entity.username or user_id} уже мониторится"
            
            # Создаем и запускаем задачу мониторинга
            task = asyncio.create_task(
                self._monitor_user_task(user_id, entity.username or str(user_id))
            )
            self.monitored_users[user_id] = task
            
            return f"Начат мониторинг пользователя {entity.username or user_id} (ID: {user_id})"
        except ValueError as e:
            logger.error(f"Ошибка при запуске мониторинга: {e}")
            return str(e)
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при запуске мониторинга: {e}")
            return f"Произошла ошибка: {str(e)}"
    
    async def stop_monitoring(self, user_identifier):
        """Останавливает мониторинг пользователя"""
        try:
            entity = await self.get_user_entity(user_identifier)
            user_id = entity.id
            
            if user_id in self.monitored_users:
                # Отменяем задачу, если она еще активна
                if not self.monitored_users[user_id].done():
                    self.monitored_users[user_id].cancel()
                
                # Закрываем незавершенные сессии
                if user_id in self.user_activity and self.user_activity[user_id]:
                    for session in self.user_activity[user_id]:
                        if session['end'] is None:
                            session['end'] = datetime.now()
                
                # Удаляем задачу из словаря
                del self.monitored_users[user_id]
                
                # Сохраняем историю активности
                await self._save_activity_history()
                
                return f"Мониторинг пользователя {entity.username or user_id} (ID: {user_id}) остановлен"
            else:
                return f"Мониторинг пользователя {entity.username or user_id} не был запущен"
        except ValueError as e:
            return str(e)
        except Exception as e:
            logger.error(f"Ошибка при остановке мониторинга: {e}")
            return f"Произошла ошибка: {str(e)}"
    
    async def _monitor_user_task(self, user_id, user_identifier):
        """Задача мониторинга пользователя"""
        logger.info(f"Начат мониторинг пользователя {user_identifier} (ID: {user_id})")
        
        last_status = None
        backoff_time = POLL_INTERVAL
        errors_count = 0
        max_errors = 5
        
        try:
            while True:
                try:
                    # Получаем entity с обновленным статусом
                    user_entity = await self.client.get_entity(user_id)
                    
                    # Проверяем статус пользователя
                    current_status = user_entity.status
                    is_online = hasattr(current_status, 'expires') and current_status.expires is not None
                    current_time = datetime.now()
                    
                    # Обновляем статус в зависимости от изменений
                    if is_online and last_status != 'online':
                        # Пользователь только что стал онлайн
                        self.user_activity[user_id].append({
                            'start': current_time,
                            'end': None
                        })
                        logger.info(f"Пользователь {user_identifier} (ID: {user_id}) онлайн в {current_time}")
                        last_status = 'online'
                    elif not is_online and last_status == 'online':
                        # Пользователь только что стал оффлайн
                        if self.user_activity[user_id] and self.user_activity[user_id][-1]['end'] is None:
                            self.user_activity[user_id][-1]['end'] = current_time
                            logger.info(f"Пользователь {user_identifier} (ID: {user_id}) оффлайн в {current_time}")
                        last_status = 'offline'
                    
                    # Сбрасываем счетчик ошибок и возвращаем интервал к нормальному
                    errors_count = 0
                    backoff_time = POLL_INTERVAL
                    
                    # Периодически сохраняем историю активности
                    if len(self.user_activity[user_id]) % 10 == 0:
                        await self._save_activity_history()
                    
                    # Ждем перед следующей проверкой
                    await asyncio.sleep(backoff_time)
                    
                except FloodWaitError as e:
                    # Обработка ограничения скорости API
                    wait_time = e.seconds
                    logger.warning(f"Сработало ограничение скорости API, ожидание {wait_time} секунд")
                    await asyncio.sleep(wait_time)
                    
                except (UserNotParticipantError, UserPrivacyRestrictedError, ChatAdminRequiredError) as e:
                    # Ошибки, связанные с отсутствием доступа к пользователю
                    logger.error(f"Нет прав для мониторинга пользователя {user_identifier}: {e}")
                    return
                    
                except Exception as e:
                    # Обработка других ошибок с экспоненциальной задержкой
                    errors_count += 1
                    logger.error(f"Ошибка при мониторинге пользователя {user_identifier}: {e}")
                    
                    if errors_count >= max_errors:
                        logger.error(f"Превышено максимальное количество ошибок ({max_errors}) для пользователя {user_identifier}")
                        return
                    
                    # Экспоненциальная задержка при ошибках
                    backoff_time = min(POLL_INTERVAL * (2 ** errors_count), 300)  # Максимум 5 минут
                    logger.info(f"Повторная попытка через {backoff_time} секунд")
                    await asyncio.sleep(backoff_time)
        
        except asyncio.CancelledError:
            logger.info(f"Мониторинг пользователя {user_identifier} отменен")
            
            # Закрываем незавершенные сессии при отмене задачи
            if user_id in self.user_activity and self.user_activity[user_id]:
                for session in self.user_activity[user_id]:
                    if session['end'] is None:
                        session['end'] = datetime.now()
            
            raise
        
        finally:
            # Сохраняем историю при завершении задачи
            await self._save_activity_history()
    
    async def generate_report(self, user_identifier):
        """Генерирует отчет об активности пользователя"""
        try:
            entity = await self.get_user_entity(user_identifier)
            user_id = entity.id
            
            if user_id not in self.user_activity or not self.user_activity[user_id]:
                return f"Данные об активности отсутствуют для пользователя {entity.username or user_id} (ID: {user_id})"
            
            user_name = entity.username or f"{entity.first_name} {getattr(entity, 'last_name', '')}"
            
            report = [f"Отчет об активности пользователя {user_name} (ID: {user_id}):\n"]
            
            # Вычисляем общую статистику
            total_sessions = len(self.user_activity[user_id])
            total_duration = datetime.timedelta(0)
            current_online = False
            
            for i, session in enumerate(sorted(self.user_activity[user_id], key=lambda x: x['start']), 1):
                start_time = session['start'].strftime("%Y-%m-%d %H:%M:%S")
                
                if session['end']:
                    if isinstance(session['end'], datetime):
                        end_time = session['end'].strftime("%Y-%m-%d %H:%M:%S")
                        duration = session['end'] - session['start']
                        duration_str = str(duration).split('.')[0]  # Форматирование без миллисекунд
                        total_duration += duration
                    else:
                        end_time = session['end']  # Для случая "В сети"
                        duration_str = "Продолжается"
                        current_online = True
                else:
                    end_time = "В сети"
                    duration_str = "Продолжается"
                    current_online = True
                
                report.append(f"{i}. Начало: {start_time}")
                report.append(f"   Конец: {end_time}")
                report.append(f"   Продолжительность: {duration_str}\n")
            
            # Добавляем итоговую статистику
            report.append("\nОбщая статистика:")
            report.append(f"Всего сессий: {total_sessions}")
            if not current_online:
                report.append(f"Общее время в сети: {str(total_duration).split('.')[0]}")
            else:
                report.append("Общее время в сети: Пользователь сейчас онлайн")
            
            return "\n".join(report)
        except ValueError as e:
            return str(e)
        except Exception as e:
            logger.error(f"Ошибка при генерации отчета: {e}")
            return f"Произошла ошибка при создании отчета: {str(e)}"
    
    async def stop_all_monitoring(self):
        """Останавливает все задачи мониторинга"""
        logger.info("Остановка всех задач мониторинга...")
        
        for user_id, task in list(self.monitored_users.items()):
            if not task.done():
                task.cancel()
            
            # Закрываем незавершенные сессии
            if user_id in self.user_activity and self.user_activity[user_id]:
                for session in self.user_activity[user_id]:
                    if session['end'] is None:
                        session['end'] = datetime.now()
        
        self.monitored_users.clear()
        await self._save_activity_history()
        logger.info("Все задачи мониторинга остановлены")

class TelegramBot:
    def __init__(self, api_id, api_hash, bot_token):
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        
        # Создание клиентов
        self.loop = asyncio.get_event_loop()
        self.client = TelegramClient('user_session', api_id, api_hash, loop=self.loop)
        self.bot = TelegramClient('bot_session', api_id, api_hash, loop=self.loop)
        
        # Инициализация сервиса мониторинга
        self.monitoring_service = None
    
    async def _check_access(self, event):
        """Проверяет доступ пользователя к боту"""
        if not ALLOWED_USERS:  # Если список пуст, разрешаем всем
            return True
        
        sender = await event.get_sender()
        return sender.id in ALLOWED_USERS or (hasattr(sender, 'username') and sender.username in ALLOWED_USERS)
    
    async def _register_handlers(self):
        """Регистрирует обработчики сообщений для бота"""
        @self.bot.on(events.NewMessage(pattern='/attack'))
        async def handle_attack(event):
            """Обработчик команды /attack"""
            if not await self._check_access(event):
                await event.respond("У вас нет доступа к этой команде.")
                return
            
            try:
                command_parts = event.message.text.split(maxsplit=1)
                if len(command_parts) != 2:
                    await event.respond("Используйте формат: /attack username или /attack user_id")
                    return
                
                user_identifier = command_parts[1].strip()
                response = await self.monitoring_service.start_monitoring(user_identifier)
                await event.respond(response)
            
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /attack: {e}")
                await event.respond(f"Произошла ошибка: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/stop'))
        async def handle_stop(event):
            """Обработчик команды /stop"""
            if not await self._check_access(event):
                await event.respond("У вас нет доступа к этой команде.")
                return
            
            try:
                command_parts = event.message.text.split(maxsplit=1)
                if len(command_parts) != 2:
                    await event.respond("Используйте формат: /stop username или /stop user_id")
                    return
                
                user_identifier = command_parts[1].strip()
                response = await self.monitoring_service.stop_monitoring(user_identifier)
                await event.respond(response)
            
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /stop: {e}")
                await event.respond(f"Произошла ошибка: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/prof'))
        async def handle_prof(event):
            """Обработчик команды /prof"""
            if not await self._check_access(event):
                await event.respond("У вас нет доступа к этой команде.")
                return
            
            try:
                command_parts = event.message.text.split(maxsplit=1)
                if len(command_parts) != 2:
                    await event.respond("Используйте формат: /prof username или /prof user_id")
                    return
                
                user_identifier = command_parts[1].strip()
                report = await self.monitoring_service.generate_report(user_identifier)
                
                # Разбиваем большие отчеты на части, если нужно
                if len(report) > 4000:
                    # Telegram имеет ограничение на длину сообщений
                    parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
                    for i, part in enumerate(parts):
                        await event.respond(f"Часть {i+1}/{len(parts)}:\n{part}")
                else:
                    await event.respond(report)
            
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /prof: {e}")
                await event.respond(f"Произошла ошибка: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/status'))
        async def handle_status(event):
            """Обработчик команды /status для получения статуса мониторинга"""
            if not await self._check_access(event):
                await event.respond("У вас нет доступа к этой команде.")
                return
            
            try:
                active_tasks = {user_id: task for user_id, task in self.monitoring_service.monitored_users.items() if not task.done()}
                
                if not active_tasks:
                    await event.respond("Нет активных задач мониторинга.")
                    return
                
                status_text = ["Активные задачи мониторинга:"]
                
                for user_id, _ in active_tasks.items():
                    # Получаем информацию о пользователе из кэша или через API
                    if user_id in self.monitoring_service.user_info_cache:
                        user_info = self.monitoring_service.user_info_cache[user_id]
                        username = user_info['username'] or f"{user_info['first_name']} {user_info['last_name'] or ''}"
                    else:
                        try:
                            user = await self.client.get_entity(user_id)
                            username = user.username or f"{user.first_name} {getattr(user, 'last_name', '')}"
                        except Exception:
                            username = f"ID: {user_id}"
                    
                    status_text.append(f"- {username} (ID: {user_id})")
                
                await event.respond("\n".join(status_text))
            
            except Exception as e:
                logger.error(f"Ошибка при обработке команды /status: {e}")
                await event.respond(f"Произошла ошибка: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/help'))
        async def handle_help(event):
            """Показывает список доступных команд"""
            help_text = """
            Доступные команды:
            /attack username - начать мониторинг пользователя (можно использовать @username или username)
            /stop username - остановить мониторинг пользователя
            /prof username - получить отчет об активности пользователя
            /status - показать список активных задач мониторинга
            /help - показать это сообщение
            
            Примеры:
            /attack durov
            /attack @durov
            /prof durov
            /stop durov
            """
            await event.respond(help_text)
    
    def _setup_signal_handlers(self):
        """Настраивает обработчики сигналов для корректного завершения"""
        def signal_handler(sig, frame):
            logger.info(f"Получен сигнал {sig}, завершение работы...")
            
            # Запускаем асинхронную задачу для остановки всех задач мониторинга
            asyncio.create_task(self._shutdown())
        
        # Регистрируем обработчики сигналов
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    async def _shutdown(self):
        """Корректно завершает работу бота"""
        logger.info("Завершение работы бота...")
        
        # Останавливаем все задачи мониторинга
        if self.monitoring_service:
            await self.monitoring_service.stop_all_monitoring()
        
        # Отключаем клиентов
        await self.bot.disconnect()
        await self.client.disconnect()
        
        # Останавливаем цикл событий
        self.loop.stop()
    
    async def start(self):
        """Запускает бота"""
        # Инициализируем клиенты
        await self.client.start()
        logger.info("Клиент Telethon запущен")
        
        await self.bot.start(bot_token=self.bot_token)
        logger.info("Бот запущен")
        
        # Инициализируем сервис мониторинга
        self.monitoring_service = UserMonitoringService(self.client)
        
        # Регистрируем обработчики сообщений
        await self._register_handlers()
        
        # Настраиваем обработчики сигналов
        self._setup_signal_handlers()
        
        # Запускаем оба клиента в одном цикле событий
        await asyncio.gather(
            self.client.run_until_disconnected(),
            self.bot.run_until_disconnected()
        )

async def main():
    """Основная функция запуска бота"""
    # Создаем и запускаем бота
    bot = TelegramBot(API_ID, API_HASH, BOT_TOKEN)
    await bot.start()

if __name__ == '__main__':
    # Убедимся, что пакет aiofiles установлен
    try:
        import aiofiles
    except ImportError:
        logger.error("Не установлен пакет aiofiles. Пожалуйста, установите его: pip install aiofiles")
        sys.exit(1)
    
    # Создаем один цикл событий и используем его для всего приложения
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Выход по запросу пользователя")
    finally:
        loop.close()