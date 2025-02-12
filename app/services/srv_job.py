from collections import Counter
from datetime import datetime
from typing import Dict, Any

from fastapi import Request

from fastapi_sqlalchemy import db

from app.helpers.exception_handler import CustomException
from app.helpers.time_int import time_int_short_day, time_int_short, now_int
from app.helpers.token_job import decode_token_job, create_token_job
from app.models import Job, Current, User
from app.models.model_total import Total
from app.models.model_transaction import Transaction
from sqlalchemy import and_, or_, update, select, insert

from app.redis_ins import set_time_redis, get_time_redis, get_redis, get_count_redis, set_count_redis
from app.schemas.sche_base import DataResponse
from app.schemas.sche_job import JobFinish, JobCancel, JobTool

detal_time = 10

r = get_redis()


class JobService(object):
    __instance = None

    @staticmethod
    def check_status():
        keys = r.keys('*')
        values_dict = {}
        for key in keys:
            typ = r.type(key)
            va = None
            if typ == "string":
                va = r.get(key)
            if typ == "hash":
                va = r.hgetall(key)
            if typ == "zset":
                va = r.zrange(key, 0, -1)
            if typ == "list":
                va = r.lrange(key, 0, -1)
            if typ == "set":
                va = r.smembers(key)
            values_dict[key] = va
        return values_dict

    @staticmethod
    def get_remain_jobs() -> Dict[str, Any]:
        jobs = db.session.query(Job).all()
        data = [{**job.__dict__, "count_today": get_count_redis(job.id)} for job in jobs]

        return DataResponse().success_response(data=data)

    @staticmethod
    def get_current_job(request: Request, imei: str, user_id: int) -> dict[str, Any]:
        # Nếu có job đang làm thì trả về job đó
        try:
            first_current = db.session.query(Current).filter_by(user_id=user_id).first()
            if first_current:
                db.session.query(Current).filter(Current.user_id == user_id).filter2(
                    Current.id != first_current.id).delete()
                db.session.commit()
                if first_current.job_id and (
                        first_current.job.finish_at is None or first_current.job.finish_at >= datetime.now()):
                    return DataResponse().success_response(
                        data={
                            "current_id": first_current.id,
                            "job": first_current.job,
                        })
                else:
                    db.session.delete(first_current)
                    db.session.commit()
            device_id = imei if (imei and imei != "unknown") else request.client.host

            # Lấy danh sách job đã làm trong {reset_day} ngày
            job_id_blocks = db.session.query(Transaction.job_id).filter(
                and_(Transaction.device_id == device_id, Transaction.time_int >= time_int_short_day())).distinct().all()

            job_id_blocks = set(job_id[0] for job_id in job_id_blocks)

            # Lọc job chưa làm trong {reset_day} ngày, chưa hết hạn
            jobs = db.session.query(Job).filter(
                and_(
                    Job.id.notin_(job_id_blocks),
                    or_(Job.finish_at.is_(None), Job.finish_at >= datetime.now()),
                    Job.is_stop.is_(False),
                )
            ).all()

            # Lọc job đã làm < max_day trong ngày
            jobs = list(filter(lambda e: e.max_day > get_count_redis(e.id) and e.count < e.total, jobs))

            if len(jobs) == 0:
                return DataResponse().success_response(data={
                    "current_id": -1,
                    "job": None,
                })
            # Chọn job có số lượng ít nhất trong ngày (* hệ số)
            min_job = min(jobs, key=lambda x: get_count_redis(x.id) * (x.factor or 1))

            current_db = Current(
                user_id=user_id,
                job_id=min_job.id
            )
            db.session.add(current_db)
            db.session.commit()
            db.session.refresh(current_db)
            return DataResponse().success_response(data={
                "current_id": current_db.id,
                "job": current_db.job,
            })
        except Exception as e:
            raise CustomException(http_code=400, code='400', message=f"error {e}")

    @staticmethod
    def start(job_id: int, user_id: int, current_id: int) -> dict[str, Any]:
        job_db = db.session.query(Job).filter_by(id=job_id).first()
        user_db = db.session.query(User).filter_by(id=user_id).first()
        if not job_db or not user_db:
            raise CustomException(http_code=400, code='400', message="job or user not found")
        set_time_redis(user_id=user_id)
        return DataResponse().success_response({
            "token": create_token_job(job_id=job_id, user_id=user_id, current_id=current_id),
            "key": job_db.key_page,
        })

    @staticmethod
    def finish(request: Request, job_finish: JobFinish) -> dict[str, Any]:
        token_job = decode_token_job(token=job_finish.token)
        job_db = db.session.query(Job).filter_by(id=token_job.job_id).first()
        current_db = db.session.query(Current).filter_by(id=token_job.current_id).first()

        if not job_db or not current_db:
            error = "job or current not found"
        elif job_finish.value_page != job_db.value_page or job_db.value_page is None:
            error = "value page is not correct"
        elif check_time_out(user_id=token_job.user_id, job_time=job_db.time):
            error = "Time out"
        else:
            error = None
        if error:
            raise CustomException(http_code=400, code='400', message=error)

        transaction = Transaction(user_id=token_job.user_id, job_id=token_job.job_id, ip=request.client.host,
                                  device_id=job_finish.imei, money=job_db.money,
                                  time_int=time_int_short(reset_day=job_db.reset_day))
        db.session.add(transaction)
        db.session.delete(current_db)
        db.session.commit()
        db.session.refresh(transaction)

        transactions = db.session.query(Transaction).filter_by(user_id=token_job.user_id).all()
        qr = update(Job).where(Job.id == transaction.job_id).values(count=Job.count + 1)
        db.session.execute(qr)
        first_total = db.session.query(Total).filter_by(user_id=token_job.user_id).first()
        if first_total:
            qr_total = update(Total).where(Total.user_id == token_job.user_id) \
                .values(count_transaction=transactions.__len__(), total=sum(int(e.money) for e in transactions),
                        count_job=set(e.job_id for e in transactions).__len__())
        else:
            qr_total = insert(Total).values(user_id=token_job.user_id, count_transaction=transactions.__len__(),
                                            total=sum(int(e.money) for e in transactions),
                                            count_job=set(e.job_id for e in transactions).__len__())
        db.session.execute(qr_total)
        db.session.commit()
        set_count_redis(job_id=token_job.job_id)
        return DataResponse().success_response(data={})

    @staticmethod
    def finish_tool(job_tools: list[JobTool]) -> dict[str, Any]:

        transactions = [Transaction(user_id=job_tool.user_id, job_id=job_tool.id, ip=job_tool.ip,
                                    device_id=job_tool.imei, created_at=job_tool.created_at,
                                    money=0, description=job_tool.description,
                                    time_int=time_int_short(reset_day=1, dt=job_tool.created_at)) for job_tool in job_tools]

        if not transactions:
            raise CustomException(http_code=400, code='400', message="job or user not found")

        db.session.bulk_save_objects(transactions)
        db.session.commit()
        id_counts = Counter(item.job_id for item in transactions)
        jobs = db.session.query(Job).filter(Job.id.in_(id_counts.keys())).all()
        for job in jobs:
            job.count += id_counts[job.id]
            set_count_redis(job_id=job.id, count=id_counts[job.id])

        db.session.commit()
        return DataResponse().success_response(data="")

    @staticmethod
    def cancel(request: Request, user_id: int, job_cancel: JobCancel) -> dict[str, Any]:
        current_db = db.session.query(Current).filter_by(user_id=user_id).first()
        if current_db:
            db.session.delete(current_db)
        transaction = Transaction(user_id=user_id, job_id=current_db.job_id, ip=request.client.host,
                                  device_id=job_cancel.imei, money=0, time_int=time_int_short(reset_day=1))
        db.session.add(transaction)
        db.session.commit()

        return DataResponse().success_response({})


def check_time_out(user_id: int, job_time: int) -> bool:
    if not get_time_redis(user_id):
        return True
    current_time = now_int()
    diff = current_time - get_time_redis(user_id)
    return not (diff - detal_time < job_time < diff + detal_time)
