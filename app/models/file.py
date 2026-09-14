from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.core.database import Base


class FileRecord(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True, index=True)
    file_id = Column(String(64), unique=True, index=True, nullable=False)
    filename = Column(String(255), nullable=False)
    size = Column(Integer, nullable=False)
    path = Column(String(512), nullable=False)
    content_type = Column(String(128), nullable=True)
    uploader_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    uploader = relationship("User", backref="uploaded_files")

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "size": self.size,
            "path": self.path,
            "content_type": self.content_type,
            "uploader_id": self.uploader_id,
            "uploader_username": self.uploader.username if self.uploader else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
