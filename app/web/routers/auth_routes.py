from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.web.auth import check_password, csrf_protect, get_csrf_token
from app.web.templating import templates

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": get_csrf_token(request),
        "error": None,
    })


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login_submit(request: Request, password: str = Form(...)):
    if check_password(password):
        request.session["authenticated"] = True
        get_csrf_token(request)
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": get_csrf_token(request),
        "error": "Неверный пароль",
    }, status_code=401)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)