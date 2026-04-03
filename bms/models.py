from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

class TokenThreeDSRequest(BaseModel):
    cid: str
    mid: str
    AppKey: str
    IsTest: bool = True
    AppType: str = "1"
    Password: str
    UserName: str

class TokenThreeDSResponse(BaseModel):
    Msg: List[str]
    Token: str
    ApiKey: str
    verbiage: Optional[str] = None
    ResponseCode: int = 200
    displayMessage: Optional[str] = None

class TransactionSaleRequest(BaseModel):
    CVN: str
    cid: str
    mid: str
    Amount: str
    AppKey: str
    Source: str = "AppClient"
    AppType: str = "1"
    ExpDate: str
    ZipCode: str
    Password: str
    UserName: str
    CardNumber: str
    NameOnCard: str
    SecureData: Optional[str] = None
    OrderReference: str
    TransactionType: str = "1"
    CardDifferenceAmount: str = "0.00"
    UserTransactionNumber: str

class PaymentPlanInfo(BaseModel):
    PlanId: int = 2
    PlanName: str = "Premier Plan"
    MonthlyFee: float = 10.0
    Description: str = "Plan 2 (Competing vs Square and Paypal)"
    DisplayName: bool = True
    SwipeDiscount: float = 1.89
    NonSwipeDiscount: float = 3.25
    SwipeTransactionFee: float = 0.2
    NonSwipeTransactionFee: float = 0.2

class TransactionSaleResponse(BaseModel):
    cv: str = "BAD"
    Msg: List[str]
    avs: str = "BAD"
    Token: Optional[str] = None
    Balance: Optional[str] = None
    CardType: str = "VISA"
    LastFour: str
    verbiage: str
    CustomerId: int = 0
    msoft_code: str
    phard_code: str
    ResponseCode: int = 200
    displayMessage: Optional[str] = None
    PaymentPlanInfo: PaymentPlanInfo
    AuthorizationNumber: str
    ServiceReferenceNumber: str

class ErrorResponse(BaseModel):
    Msg: List[str]
    ResponseCode: int
    verbiage: Optional[str] = "ERROR"
    displayMessage: Optional[str] = None