from models.models import (User, Client, Service,
                           Master, WorkingHours,
                           Appointment, Organization,
                           Membership, MembershipRole,
                           Invitation, PasswordResetToken,
                           RefreshToken, ActivityLog, MasterService,
                           TimeOff, WorkingHoursException,
                           EmailVerificationToken, ClientComment,
                           AiReport,
                           AppointmentToken)

from models.models import Payment  # noqa: F401  (таблица платежей)
from models.notification import Notification  # noqa: F401  (уведомления в CRM)
from models.ai_chat import AiChatMessage  # noqa: F401  (счётчик ИИ-чата)
from models.models import WaitlistEntry  # noqa: F401  (лист ожидания)
from models.models import AppointmentParticipant  # noqa: F401  (ещё клиенты в записи)
from models.models import ClientStatus  # noqa: F401  (статусы клиентов)
from models.models import TranslationCache  # noqa: F401  (кэш автопереводов услуг)
