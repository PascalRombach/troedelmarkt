from fastapi import FastAPI, Depends, Response, Body, Request, Security, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security.api_key import APIKeyBase, APIKey, APIKeyIn
from .database import session_factory, Base, engine
from .schemas import *
from .ormmodels import Seller
from . import data_interaction as crud
from sqlalchemy.orm import Session
from decimal import Decimal
from threading import Lock
from io import StringIO
from csv import writer as csv_writer
from hmac import compare_digest
from .config_reader import CONFIG
from .transaction_logger import TransactionLogger
import jwt
from time import time as timestamp
from typing import Literal

Base.metadata.create_all(engine)

def get_session():
    session = session_factory()
    with database_lock:
        try:
            yield session
        finally:
            session.close()

transaction_logger = TransactionLogger()
transaction_logger.start()
app = FastAPI()
database_lock = Lock()
REGISTERED_TOKENS : set[str] = set()

class CustomKeyAuthorisation(APIKeyBase):
    def __init__(self,*,description: str|None=None):
        self.scheme_name = "Bearer"
        self.model: APIKey = APIKey(
            **{"in":APIKeyIn.header}, 
            # Python doesn't accept "in" as a kwarg
            name="Authorization"
        )
    
    @staticmethod
    def is_authorised(token: str) -> bool:
        for reg_token in REGISTERED_TOKENS:
            if compare_digest(reg_token,token):
                return True
        return False
    
    def __call__(self, request: Request) -> Literal[True]:
        api_key = request.headers.get(self.model.name)
        if api_key is None or not self.is_authorised(api_key):
            raise HTTPException(
                401,
                detail="Bearer token is invalid",
                headers={"WWW-Authentication":self.model.name}
            )
        return True

@app.get("/sellers", responses={
    401 : {"description":"The bearer token is invalid or not passed"}
})
def ep_get_sellers(
    session: Session = Depends(get_session),
    _=Security(CustomKeyAuthorisation())
):
    return [
        SellerServerModel.from_sql(seller) 
        for seller in  crud.get_all_sellers(session)
    ]

@app.post("/seller", responses={
    401 : {"description":"The bearer token is invalid or not passed"},
    409 : {"description":"A seller with the specified id already exists"}
})
def ep_post_seller(
    seller: SellerClientModel, 
    session: Session = Depends(get_session),
    _=Security(CustomKeyAuthorisation())
):
    try:
        return SellerServerModel.from_sql(
            crud.new_seller(session,seller)
        )
    except RuntimeError:
        return Response("A seller with this id already exists",409)

@app.get("/sellers/{seller_id}", responses={
    401 : {"description":"The bearer token is invalid or not passed"},
    404 : {"description":"There is no seller with the specified id"}
})
def ep_get_seller(
    seller_id: str, 
    session: Session = Depends(get_session),
    _=Security(CustomKeyAuthorisation())
):
    seller = crud.get_seller(session,seller_id)
    if seller is None:
        return Response(
            f"A seller with the id {seller_id} doesn't exist",
            404
        )
    return SellerServerModel.from_sql(seller)

@app.delete("/seller/{seller_id}", responses={
    401 : {"description":"The bearer token is invalid or not passed"},
    403 : {"description":"The seller specified has a balance that is not 0"},
    404 : {"description":"There is no seller with the specified id"}
})
def ep_delete_seller(
    seller_id: str, 
    session: Session = Depends(get_session),
    _=Security(CustomKeyAuthorisation())
):  
    seller = crud.get_seller(session,seller_id)
    if seller is None:
        return Response(
            f"A seller with the id {seller_id} doesn't exist",
            404
        )
    if Decimal(seller.balance) != 0:
        return Response(
            "Seller balance is non-null. May not delete",
            403
        )
    crud.remove_seller(session,seller)

    return SellerServerModel.from_sql(seller)
    pass

@app.patch("/seller/{seller_id}", responses={
    400 : {"description":"One key of the object is invalid"},
    404 : {"description":"There is no seller with the specified id"}
})
def ep_patch_seller(
    seller_id: str, 
    modification: SellerModifyModel, 
    session: Session = Depends(get_session),
    _=Security(CustomKeyAuthorisation())
): 
    return SellerServerModel.from_sql(
        crud.update_seller(session,seller_id,modification)
    )


@app.post("/sell", responses={
    400 : {
        "description":"Some part of the request was not parsable. This cancels the entire transaction",
        "model":MalformedTransactionResponse
    },
    401 : {"description":"The bearer token is invalid or not passed"}
})
def ep_sell(
    request: Request,
    items: list[ItemModel], 
    session: Session = Depends(get_session),
    _=Security(CustomKeyAuthorisation())
):
    sql_sellers = {
        seller.id: seller 
        for seller in crud.get_all_sellers(session)
    }
    
    error_message: MalformedTransactionResponse = {}
    # Add reference to non-existing sellers
    unknown_sellers = {item.sellerId for item in items}\
        .difference(sql_sellers.keys())
    for seller_id in unknown_sellers:
        relevant_index = next(filter(
            lambda item: item[1].sellerId == seller_id,
            enumerate(items)
        ))[0]
        error_message.setdefault(str(relevant_index),([],[]))
        error_message[relevant_index][0].append("sellerId")
    
    if len(error_message) > 0:
        return Response(error_message,400)
    
    relevant_sellers: set[Seller] = set()
    for item in items:
        seller = sql_sellers[item.sellerId]
        relevant_sellers.add(seller)
        
        balance = Decimal(seller.balance)
        seller.balance = str(balance+item.price)
    session.commit()
    transaction_logger.add_transaction(
        *items,
        origin_ip=request.client.host
    )
    
    return [
        SellerServerModel.from_sql(seller) 
        for seller in relevant_sellers
    ]

@app.get("/exportcsv", responses={
    401 : {"description":"The bearer token is invalid or not passed"}
})
def ep_exportcsv(
    session: Session = Depends(get_session),
    _=Security(CustomKeyAuthorisation())
):
    sellers = crud.get_all_sellers(session)
    io = StringIO()
    writer = csv_writer(io)
    writer.writerow(("Trader ID","Name","Sum of all Sales","Provision Rate","Total Provision","Trader earnings"))
    for s in sellers:
        balance = Decimal(s.balance)
        rate = Decimal(s.rate)
        writer.writerow((
            s.id,
            s.name,
            balance,
            rate,
            balance*rate,
            balance*(1-rate)
        ))
        pass

    io.seek(0)
    return StreamingResponse(io,media_type="text/csv")

@app.get("/teapot",responses={418:{"description":"This is a teapot"}})
def ep_teapot():
    return Response("I'm a teapot",418)

@app.post("/login", responses={
    200 : {"example":"Bearer ewuiZHrhkÖefdi5748ogrhekl"},
    401 : {"description":"The password passed is incorrect"}
})
def ep_login(request: Request, password: str = Body(media_type="text/plain")):
    if not compare_digest(password,CONFIG["service"]["pswd"]):
        return Response("The password you entered is incorrect",401)
    
    global login_incrementer
    token = jwt.encode({
        "host":request.client.host,
        "timestamp":timestamp()
    },CONFIG["service"]["secret"],"HS256")
    token = f"Bearer {token}"
    REGISTERED_TOKENS.add(token)
    return Response(token,200,media_type="text/plain")