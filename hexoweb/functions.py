import json
import logging
import os
import re
import shutil
import tarfile
from datetime import timezone, timedelta, date, datetime
from html import escape
from time import strftime, localtime, time, sleep
from zlib import crc32 as zlib_crc32

import github
import html2text as ht
import requests
import unicodedata
import yaml
from bs4 import BeautifulSoup
from django.core.management import execute_from_command_line
from django.template.defaulttags import register
from markdown import markdown
from urllib3 import disable_warnings
from urllib.parse import quote, unquote, urlparse

import hexoweb.libs.i18n
from core.qexoSettings import ALL_SETTINGS
from core.qexoSettings import QEXO_VERSION, QEXO_STATIC, VDITOR_LANGUAGES
from core.settings import DATABASES
from hexoweb.libs.elevator import elevator
from hexoweb.libs.onepush import notify
from hexoweb.libs.platforms import get_provider
from hexoweb.libs.i18n import get_language
from .models import Cache, SettingModel, FriendModel, NotificationModel, CustomModel, StatisticUV, StatisticPV, \
    ImageModel, TalkModel, PostModel

disable_warnings()

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] %(levelname)s: %(message)s(%(filename)s.%(funcName)s[line:%(lineno)d])',
                    datefmt="%d/%b/%Y %H:%M:%S")


def get_setting(name):
    try:
        return SettingModel.objects.get(name=name).content
    except Exception:
        return ""


def update_language():
    global _Language
    _Language = get_setting("LANGUAGE")
    if not _Language:
        save_setting("LANGUAGE", "zh_CN")
        _Language = "zh_CN"
    _Language = get_language(_Language).default
    return _Language


try:
    _Language = update_language()
except Exception:
    logging.error("Language获取失败, 默认为zh_CN")
    _Language = get_language("zh_CN").default


def Language():
    try:
        return _Language
    except Exception:
        logging.error(gettext("ERROR_GETTING_PROVIDER") + ": " + gettext("RETRY"))
        return update_language()


@register.filter
def gettext(value):
    return Language()["data"].get(value, value)


def update_provider():
    global _Provider
    _provider = json.loads(get_setting("PROVIDER"))
    _Provider = get_provider(_provider["provider"], **_provider["params"])
    return _Provider


try:
    _Provider = update_provider()
except Exception:
    logging.error(gettext("ERROR_GETTING_PROVIDER") + ": " + gettext("JUMPED"))


def Provider():
    try:
        return _Provider
    except Exception:
        logging.error(gettext("ERROR_GETTING_PROVIDER") + ": " + gettext("RETRY"))
        return update_provider()


@register.filter  # 在模板中使用range()
def get_range(value):
    return range(1, value + 1)


@register.filter
def div(value, _div):  # 保留两位小数的除法
    return round((value / _div), 2)


@register.filter
def urlencoder(value):
    return quote(value)


@register.filter
def excerpt(value, length):
    return value if len(value) <= length else value[0:length - 1] + "..."


def get_cdn():
    cdn_prev = get_setting("CDN_PREV")
    if not cdn_prev:
        cdn_prev = "https://registry.npmmirror.com/qexo-static/{version}/files/qexo"
        for i in ALL_SETTINGS:
            if i[0] == "CDN_PREV":
                cdn_prev = i[1]
                break
        save_setting("CDN_PREV", cdn_prev)
    return cdn_prev.format(version=QEXO_STATIC)


# def get_cdnjs():
#     cdnjs = get_setting("CDNJS")
#     if not cdnjs:
#         save_setting("CDNJS", "https://cdn.staticfile.org/")
#         cdnjs = "https://cdn.staticfile.org/"
#     return cdnjs


# 获取用户自定义的样式配置
def get_custom_config():
    context = {"cdn_prev": get_cdn(), "QEXO_NAME": get_setting("QEXO_NAME"),
               "language": Language().get("name", "zh_CN"), "vditor_languages": VDITOR_LANGUAGES,
               "all_languages": hexoweb.libs.i18n.all_languages()}
    if not context["QEXO_NAME"]:
        save_setting('QEXO_NAME', 'Hexo' + gettext("CONSOLE"))
        context["QEXO_NAME"] = get_setting("QEXO_NAME")
    context["QEXO_SPLIT"] = get_setting("QEXO_SPLIT")
    if not context["QEXO_SPLIT"]:
        save_setting('QEXO_SPLIT', ' - ')
        context["QEXO_SPLIT"] = get_setting("QEXO_SPLIT")
    context["QEXO_LOGO"] = get_setting("QEXO_LOGO")
    if not context["QEXO_LOGO"]:
        save_setting('QEXO_LOGO',
                     'https://unpkg.com/qexo-static@' + QEXO_STATIC + '/qexo/images/qexo.png')
        context["QEXO_LOGO"] = get_setting("QEXO_LOGO")
    context["QEXO_LOGO_DARK"] = get_setting("QEXO_LOGO_DARK")
    if not context["QEXO_LOGO_DARK"]:
        save_setting('QEXO_LOGO_DARK',
                     'https://unpkg.com/qexo-static@' + QEXO_STATIC + '/qexo/images/qexo-dark.png')
        context["QEXO_LOGO_DARK"] = get_setting("QEXO_LOGO_DARK")
    context["QEXO_ICON"] = get_setting("QEXO_ICON")
    if not context["QEXO_ICON"]:
        save_setting('QEXO_ICON',
                     'https://unpkg.com/qexo-static@' + QEXO_STATIC + '/qexo/images/icon.png')
        context["QEXO_ICON"] = get_setting("QEXO_ICON")
    return context


# 更新缓存
def update_caches(name, content, _type="json"):
    caches = Cache.objects.filter(name=name)
    if caches.count():
        caches.delete()
    posts_cache = Cache()
    posts_cache.name = name
    if _type == "json":
        posts_cache.content = json.dumps(content)
    else:
        posts_cache.content = content
    posts_cache.save()
    logging.info(gettext("REBUILD_CACHE_SUCCESS").format(name))


def _filter_items_by_search(items, search_term):
    """过滤列表项，只保留名称中包含搜索词的项目"""
    if not search_term:
        return items

    filtered_items = []
    for item in items:
        if search_term.upper() in item["name"].upper():
            filtered_items.append(item)
    return filtered_items


def _get_cached_or_fresh_data(cache_name, provider_method, search_term=None):
    """从缓存获取数据或通过provider获取新数据"""
    # 检查是否有现有缓存
    old_cache = Cache.objects.filter(name=cache_name)

    # 如果没有缓存或需要搜索，获取完整结果
    if not old_cache.count() or search_term:
        try:
            if old_cache.count():
                # 有缓存但需要搜索，先尝试从缓存过滤
                cached_data = json.loads(old_cache.first().content)
                filtered_data = _filter_items_by_search(cached_data, search_term)
                update_caches(f"{cache_name}.{search_term}", filtered_data)
                return filtered_data
        except Exception:
            pass

        # 获取新数据
        results = provider_method()
        update_caches(cache_name, results)

        if search_term:
            filtered_results = _filter_items_by_search(results, search_term)
            update_caches(f"{cache_name}.{search_term}", filtered_results)
            return filtered_results
        return results

    # 直接返回缓存
    try:
        return json.loads(old_cache.first().content)
    except Exception:
        results = provider_method()
        update_caches(cache_name, results)
        return results


def update_posts_cache(s=None):
    return _get_cached_or_fresh_data("posts", Provider().get_posts, s)


def update_pages_cache(s=None):
    return _get_cached_or_fresh_data("pages", Provider().get_pages, s)


def update_configs_cache(s=None):
    return _get_cached_or_fresh_data("configs", Provider().get_configs, s)


def delete_all_caches():
    caches = Cache.objects.all()
    for cache in caches:
        if cache.name != "update":
            cache.delete()
    logging.info(gettext("PURGE_ALL_CACHE_SUCCESS"))


def save_setting(name, content):
    name = unicodedata.normalize('NFC', name)
    content = unicodedata.normalize('NFC', content)
    obj = SettingModel.objects.filter(name=name)
    if obj.count() == 1:
        obj.delete()
    if obj.count() > 1:
        for i in obj:
            i.delete()
    new_set = SettingModel()
    new_set.name = str(name)
    if content is not None:
        new_set.content = str(content)
    else:
        new_set.content = ""
    new_set.save()
    logging.info(gettext("SAVE_SETTING") + "{} => {}".format(name, content if name != "PROVIDER" else "******"))
    return new_set


def save_custom(name, content):
    name = unicodedata.normalize('NFC', name)
    content = unicodedata.normalize('NFC', content)
    obj = CustomModel.objects.filter(name=name)
    if obj.count() == 1:
        obj.delete()
    if obj.count() > 1:
        for i in obj:
            i.delete()
    new_set = CustomModel()
    new_set.name = str(name)
    if content is not None:
        new_set.content = str(content)
    else:
        new_set.content = ""
    new_set.save()
    logging.info(gettext("SAVE_CUSTOM") + "{} => {}".format(name, content))
    return new_set


def get_latest_version():
    context = dict()
    try:
        provider = json.loads(get_setting("PROVIDER"))
        if provider["provider"] == "github":
            user = github.Github(provider["params"]["token"])
            latest = user.get_repo("am-abudu/Qexo").get_latest_release()
            if latest.tag_name and (latest.tag_name != QEXO_VERSION):
                context["hasNew"] = True
            else:
                context["hasNew"] = False
            context["newer"] = latest.tag_name
            context["newer_link"] = latest.html_url
            context["newer_time"] = latest.created_at.astimezone(
                timezone(timedelta(hours=8))).strftime(
                "%Y-%m-%d %H:%M:%S")
            context["newer_text"] = markdown(latest.body).replace("\n", "")
            context["status"] = True
            logging.info(gettext("GET_UPDATE_SUCCESS") + ": {} {}".format(latest.tag_name, context["newer_time"]))
        else:
            latest = requests.get("https://api.github.com/repos/Qexo/Qexo/releases/latest").json()
            logging.info(gettext("GET_UPDATE_SUCCESS") + ": {}".format(latest["tag_name"]))
            if latest["tag_name"] and (latest["tag_name"] != QEXO_VERSION):
                context["hasNew"] = True
            else:
                context["hasNew"] = False
            context["newer"] = latest["tag_name"]
            context["newer_link"] = latest["html_url"]
            context["newer_time"] = datetime.strptime(latest["created_at"], "%Y-%m-%dT%H:%M:%SZ").astimezone(
                timezone(timedelta(hours=16))).strftime(
                "%Y-%m-%d %H:%M:%S")
            context["newer_text"] = markdown(latest["body"]).replace("\n", "")
            context["status"] = True
    except Exception as e:
        logging.error(gettext("GET_UPDATE_FAILED") + ": " + repr(e))
        context["status"] = False
    return context


def check_if_api_auth(request):
    if request.POST.get("token") == get_setting("WEBHOOK_APIKEY"):
        return True
    if request.GET.get("token") == get_setting("WEBHOOK_APIKEY"):
        return True
    logging.info(
        request.path + ":" + gettext("API_VERIFY_FAILED").format(
            request.META['HTTP_X_FORWARDED_FOR'] if 'HTTP_X_FORWARDED_FOR' in request.META.keys() else request.META[
                'REMOTE_ADDR']))
    return False


def check_if_vercel():
    if os.environ.get("VERCEL"):
        return True
    if get_setting("FORCE_VERCEL"):
        return True
    return False


def check_if_docker():
    if os.environ.get("DOCKER"):
        return True
    if get_setting("FORCE_DOCKER"):
        return True
    return False


def get_crc16(x, _hex=False):
    x = str(x)
    a = 0xFFFF
    b = 0xA001
    for byte in x:
        a ^= ord(byte)
        for i in range(8):
            last = a % 2
            a >>= 1
            if last == 1:
                a ^= b
    s = hex(a)
    return str(int(s[2:4] + s[4:6], 16)) if _hex is False else (s[2:4] + s[4:6])


def get_crc32(x, _hex=False):
    return str(zlib_crc32(x.encode("utf8"))) if _hex is False else hex(
        zlib_crc32(x.encode("utf8")))[2:]


def get_crc_by_time(_strtime, alg, rep):
    if rep == "hex":
        use_hex = True
    else:
        use_hex = False
    if alg != "crc16" and alg != "crc32":
        return ""
    return get_crc16(_strtime.replace(".", "0"), _hex=use_hex) if alg == "crc16" else get_crc32(
        _strtime.replace(".", "0"), _hex=use_hex)


def fix_all(all_settings=ALL_SETTINGS):
    counter = 0
    already = list()
    deleted = list()
    additions = list()
    settings = SettingModel.objects.all()
    for query in settings:
        if query.name not in already:
            already.append(query.name)
        else:
            deleted.append(query.name)
            query.delete()
            counter += 1
    for setting in all_settings:
        if (setting[0] not in already) or (setting[2]):
            additions.append(setting[0])
            save_setting(setting[0], setting[1])
            counter += 1
    logging.info(gettext("FIX_SUCCESS").format(counter))
    logging.info(gettext("DEL_VALUE") + str(deleted))
    logging.info(gettext("FIX_VALUE") + str(additions))
    return counter


def get_project_detail():
    return {"token": get_setting("VERCEL_TOKEN"),
            "id": get_setting("PROJECT_ID")}


def checkBuilding(projectId, token):
    r = 0
    url = "https://api.vercel.com/v6/deployments/?projectId=" + projectId
    header = dict()
    header["Authorization"] = "Bearer " + token
    header["Content-Type"] = "application/json"
    response = requests.get(url, headers=header).json()
    result = response["deployments"]
    for deployment in result:
        if deployment['state'] == "BUILDING" or deployment['state'] == "INITIALIZING":
            r += 1
    return r


def file_get_contents(file):
    with open(file, 'r', encoding="utf8") as f:
        logging.info(gettext("READ_FILE") + ": " + file)
        content = f.read()
    return content


def getEachFiles(base, path=""):
    file = list()
    handler = os.listdir(base + "/" + path)
    for item in handler:
        if item != '.git':
            fromfile = base + "/" + path + "/" + item
            if os.path.isdir(fromfile):
                file += getEachFiles(base, path + "/" + item)
            else:
                file.append({"file": path + "/" + item,
                             "data": file_get_contents(fromfile)})
    return file


def getIndexFile(base, path=""):
    index = ""
    handler = os.listdir(base + "/" + path)
    for item in handler:
        if item != 'manage.py':
            fromfile = base + "/" + path + "/" + item
            if os.path.isdir(fromfile):
                tmp = getIndexFile(base, path + "/" + item)
                if tmp:
                    index = tmp
        else:
            index = path
            break
    return index


def get_update_url(target):
    all_updates = json.loads(get_setting("ALL_UPDATES"))
    for update in all_updates:
        if update["name"] == target:
            return update["url"]
    return False


def VercelUpdate(appId, token, sourcePath=""):
    if checkBuilding(appId, token):
        logging.error(gettext("UPDATE_QUEUING"))
        return {"status": False, "msg": gettext("UPDATE_QUEUING")}
    url = "https://api.vercel.com/v13/deployments"
    header = dict()
    data = dict()
    header["Authorization"] = "Bearer " + token
    header["Content-Type"] = "application/json"
    data["name"] = "qexo"
    data["project"] = appId
    data["target"] = "production"
    if sourcePath == "":
        sourcePath = os.path.abspath("")
    data["files"] = getEachFiles(sourcePath)
    response = requests.post(url, data=json.dumps(data), headers=header)
    logging.info(gettext("UPDATE_SUCCESS") + ": " + response.text)
    filelist = os.listdir("/tmp")
    logging.info(gettext("START_DEL"))
    for filename in filelist:  # delete all files except tmp
        try:
            if os.path.isfile("/tmp/" + filename):
                os.remove("/tmp/" + filename)
            elif os.path.isdir("/tmp/" + filename):
                shutil.rmtree("/tmp/" + filename)
            else:
                pass
        except Exception as e:
            logging.error(gettext("DEL_FAILED") + ": " + repr(e))
    return {"status": True, "msg": response.json()}


def VercelOnekeyUpdate(url):
    logging.info(gettext("START_VERCEL_UPDATE"))
    vercel_config = get_project_detail()
    tmpPath = '/tmp'
    # 从github下载对应tar.gz，并解压
    # logging.info("download from " + url)
    _tarfile = tmpPath + '/github.tar.gz'
    with open(_tarfile, "wb") as file:
        file.write(requests.get(url).content)
    logging.info(gettext("START_EXTRACT_UPDATE"))
    # logging.info("ext files")
    t = tarfile.open(_tarfile)
    t.extractall(path=tmpPath)
    t.close()
    os.remove(_tarfile)
    logging.info(gettext("FIND_UPDATE_INDEX"))
    outPath = os.path.abspath(tmpPath + getIndexFile(tmpPath))
    # logging.info("outPath: " + outPath)
    if outPath == '':
        return {"status": False, "msg": gettext("FIND_INDEX_FAILED")}
    logging.info(gettext("FIND_INDEX_SUCCESS") + ": " + outPath)
    return VercelUpdate(vercel_config["id"], vercel_config["token"], outPath)


def copy_all_files(src_dir, dst_dir):
    if not os.path.exists(dst_dir):
        os.makedirs(dst_dir)
    for file in os.listdir(src_dir):
        file_path = os.path.join(src_dir, file)
        dst_path = os.path.join(dst_dir, file)
        if os.path.exists(dst_path):
            continue
        if os.path.isfile(os.path.join(src_dir, file)):
            shutil.copyfile(file_path, dst_path)
        else:
            shutil.copytree(file_path, dst_path)


def pip_main(args):
    try:
        import pip
    except ImportError:
        raise 'pip is not installed'
    try:
        func = pip.main
    except AttributeError:
        from pip._internal import main as func

    func(args)


def LocalOnekeyUpdate(url):
    import threading
    logging.info(gettext("START_LOCAL_UPDATE"))
    Path = os.path.abspath("")
    tmpPath = os.path.abspath("./_tmp")
    if os.path.exists(tmpPath):
        shutil.rmtree(tmpPath)
    os.mkdir(tmpPath)
    _tarfile = tmpPath + '/github.tar.gz'
    with open(_tarfile, "wb") as file:
        file.write(requests.get(url).content)
    logging.info(gettext("START_EXTRACT_UPDATE"))
    t = tarfile.open(_tarfile)
    t.extractall(path=tmpPath)
    t.close()
    os.remove(_tarfile)
    outPath = os.path.abspath(tmpPath + getIndexFile(tmpPath))
    logging.info(gettext("FIND_INDEX_SUCCESS") + ": " + outPath)
    filelist = os.listdir(Path)
    logging.info(gettext("START_DEL"))
    for filename in filelist:  # delete all files except tmp
        if filename not in ["_tmp", "configs.py", "db"]:
            if os.path.isfile(filename):
                os.remove(filename)
            elif os.path.isdir(filename):
                shutil.rmtree(filename)
            else:
                pass
    logging.info(gettext("START_COPY"))
    copy_all_files(outPath, Path)
    os.chmod(Path + "/entrypoint.sh", 0o755)
    logging.info(gettext("DEL_TMP"))
    shutil.rmtree(tmpPath)
    logging.info(gettext("UPDATE_LIB"))
    if check_if_docker():
        pip_main(['install', '-r', 'requirements-slim.txt'])
    else:
        pip_main(['install', '-r', 'requirements.txt'])
    logging.info(gettext("MIGRATE_DB"))
    execute_from_command_line(['manage.py', 'makemigrations'])
    execute_from_command_line(['manage.py', 'migrate'])
    logging.info(gettext("LOCAL_UPDATE_SUCCESS"))
    t = threading.Thread(target=lambda: rerun(5))
    t.start()
    return {"status": True, "msg": gettext("UPDATE_SUCCESS")}


def is_runserver():
    import sys
    current_command = sys.argv[1] if len(sys.argv) > 1 else ''
    return current_command == 'runserver'


def rerun(wait):
    sleep(wait)
    if is_runserver():
        os._exit(3)
    else:
        import signal
        os.kill(os.getpid(), signal.SIGHUP)


def CreateNotification(label, content, now):
    N = NotificationModel()
    N.label = label
    N.content = content
    N.time = str(float(now))
    N.save()
    try:
        notify_me(label, content)
    except Exception:
        pass
    return N


def GetNotifications():
    N = NotificationModel.objects.all()
    result = list()
    for notification in N:
        result.append(dict(
            label=notification.label,
            content=notification.content.replace("\n", "<br>").replace("<p>", "<p class=\"text-sm mb-0\">"),
            timestamp=notification.time,
            time=strftime("%Y-%m-%d %H:%M:%S", localtime(float(notification.time)))
        ))
    return result


def DelNotification(_time):
    N = NotificationModel.objects.get(time=_time)
    N.delete()
    return N


def notify_me(title, content):
    config = get_setting("ONEPUSH")
    if config:
        config = json.loads(config)
    else:
        return False
    if config["params"].get("mdFormat") == "true":
        text_maker = ht.HTML2Text()
        text_maker.bypass_tables = False
        content = text_maker.handle(content)
    ntfy = notify(config["notifier"], **config["params"], title=gettext("QEXO_MSG") + ": " + title, content=content)
    try:
        return ntfy.text
    except Exception:
        # logging.info("通知类型无输出信息, 使用OK缺省")
        return "OK"


def get_domain(domain):
    return domain.split("/")[2].split(":")[0] if domain[:4] == "http" else domain.split(":")[0]


def verify_provider(provider):
    try:
        provider = get_provider(provider["provider"], **provider["params"])
        home = provider.get_path("")
        hexo = 0
        indexhtml = 0
        source = 0
        pack = 0
        theme = 0
        theme_dir = 0
        config_hexo = 0
        config_theme = 0
        status = 0
        # 校验根目录文件
        for file in home["data"]:
            if file["name"] == "index.html" and file["type"] == "file":
                indexhtml = 1
            if file["name"] == "source" and file["type"] == "dir":
                source = 1
            if file["name"] == "themes" and file["type"] == "dir":
                theme_dir = 1
            if file["name"] == "package.json" and file["type"] == "file":
                pack = "package.json"
            if file["name"] == "_config.yml" and file["type"] == "file":
                config_hexo = "_config.yml"
        # # 读取主题 校验主题配置
        # try:
        #     if config_hexo:
        #         res = provider.get_content("_config.yml")
        #         content = yaml.unsafe_load(res)
        #         if content.get("theme"):
        #             theme = str(content.get("theme"))
        #             for file in home["data"]:
        #                 if file["name"] == "_config.{}.yml".format(theme) and file["type"] == "file":
        #                     config_theme = "_config.{}.yml".format(theme)
        #                     logging.info("检测到主题配置文件: _config.{}.yml".format(theme))
        #                     break
        #             if (not config_theme) and theme_dir:
        #                 theme_path = provider.get_path("themes/" + theme)
        #                 for file in theme_path["data"]:
        #                     if file["name"] == "_config.yml" and file["type"] == "file":
        #                         config_theme = "themes/" + theme + "/_config.yml"
        #                         logging.info("检测到主题配置文件: themes/" + theme + "/_config.yml")
        #                         break
        # except Exception as e:
        #     logging.error("校验配置报错" + repr(e))
        # 校验 Package.json 及 Hexo
        if pack:
            try:
                content = json.loads(provider.get_content("package.json"))
                if content:
                    if content.get("hexo"):
                        if content["hexo"].get("version"):
                            hexo = content["hexo"].get("version")
                    if content.get("dependencies"):
                        if content["dependencies"].get("hexo"):
                            hexo = content["dependencies"].get("hexo")
            except Exception as e:
                logging.error(gettext("PROVIDER_VERIFY_ERROR") + ": " + repr(e))
        # 总结校验
        if hexo and config_hexo and (not indexhtml) and source and pack:
            status = 1
        result = {
            "status": status,
            "hexo": hexo,
            "config_hexo": config_hexo,
            "indexhtml": indexhtml,
            "source": source,
            "theme_dir": theme_dir,
            "package": pack,
        }
        logging.info(gettext("PROVIDER_VERIFY_SUCCESS").format(str(result)))
        return result
    except Exception as e:
        logging.error(gettext("PROVIDER_VERIFY_ERROR") + ": " + repr(e))
        return {"status": -1}


def get_post_details(article, safe=True):
    flag = False
    if not (article.startswith("---") or article.startswith(";;;")):
        flag = True
        if ";;;" in article:
            article = ";;;\n" + article
        elif "---" in article:
            article = "---\n" + article
        else:
            flag = False
    abbrlink = get_crc_by_time(str(time()), get_setting("ABBRLINK_ALG"), get_setting("ABBRLINK_REP"))
    dateformat = datetime.now(timezone.utc).astimezone().isoformat()
    try:
        if article[:3] == "---":
            front_matter = re.search(r"---([\s\S]*?)---", article, flags=0).group()[3:-4]
            front_matter = front_matter.replace("{{ date }}", dateformat).replace("{{ abbrlink }}", abbrlink).replace(
                "{{ slug }}",
                abbrlink).replace("{",
                                  "").replace(
                "}", "")
            front_matter = yaml.safe_load(front_matter)
        elif article[:3] == ";;;":
            front_matter = json.loads("{{{}}}".format(
                re.search(r";;;([\s\S]*?);;;", article, flags=0).group()[3:-4].replace("{{ date }}",
                                                                                       dateformat).replace(
                    "{{ abbrlink }}",
                    abbrlink).replace(
                    "{{ slug }}", abbrlink)))
        else:
            front_matter = {}
    except Exception:
        if flag:
            article = article[3:]
        return {}, repr(article).replace("<", "\\<").replace(">", "\\>").replace("!", "\\!") if safe else article
    if not isinstance(front_matter, dict) or not front_matter:
        logging.info(gettext("FRONT_MATTER_GET_ERROR").format(front_matter))
        front_matter = {}
        if flag:
            article = article[3:]
        passage = repr(article).replace("<", "\\<").replace(">", "\\>").replace("!", "\\!") if safe else article
    else:
        for key in front_matter.keys():
            if type(front_matter.get(key)) == datetime:
                front_matter[key] = front_matter[key].astimezone().isoformat()
            elif type(front_matter.get(key)) == date:
                front_matter[key] = front_matter[key].isoformat()
        if safe:
            passage = repr(re.search(r"[;-][;-][;-]([\s\S]*)", article[3:], flags=0).group()[3:]).replace("<",
                                                                                                          "\\<").replace(
                ">",
                "\\>").replace(
                "!", "\\!")
        else:
            passage = re.search(r"[;-][;-][;-]([\s\S]*)", article[3:], flags=0).group()[3:]
    return front_matter, passage


def _export_model_data(model_class, field_mapping=None):
    """
    通用导出函数，从数据库获取指定模型的所有记录并转换为字典列表

    Args:
        model_class: 要导出的模型类
        field_mapping: 字段映射函数，用于自定义每条记录如何转换为字典

    Returns:
        包含所有记录数据的字典列表
    """
    all_items = model_class.objects.all()
    result = []

    for item in all_items:
        if field_mapping:
            result.append(field_mapping(item))
        else:
            # 默认映射逻辑，获取所有字段
            item_dict = {}
            for field in item._meta.fields:
                field_name = field.name
                item_dict[field_name] = getattr(item, field_name)
            result.append(item_dict)

    return result


def export_settings():
    return _export_model_data(
        SettingModel,
        lambda item: {"name": item.name, "content": item.content}
    )


def export_images():
    return _export_model_data(
        ImageModel,
        lambda item: {
            "name": item.name,
            "url": item.url,
            "size": item.size,
            "date": item.date,
            "type": item.type,
            "deleteConfig": item.deleteConfig
        }
    )


def export_friends():
    return _export_model_data(
        FriendModel,
        lambda item: {
            "name": item.name,
            "url": item.url,
            "imageUrl": item.imageUrl,
            "time": item.time,
            "description": item.description,
            "status": item.status
        }
    )


def export_notifications():
    return _export_model_data(
        NotificationModel,
        lambda item: {"time": item.time, "label": item.label, "content": item.content}
    )


def export_customs():
    return _export_model_data(
        CustomModel,
        lambda item: {"name": item.name, "content": item.content}
    )


def export_uv():
    return _export_model_data(
        StatisticUV,
        lambda item: {"ip": item.ip}
    )


def export_pv():
    return _export_model_data(
        StatisticPV,
        lambda item: {"url": item.url, "number": item.number}
    )


def export_talks():
    return _export_model_data(
        TalkModel,
        lambda item: {"content": item.content, "tags": item.tags, "time": item.time, "like": item.like}
    )


def export_posts():
    return _export_model_data(
        PostModel,
        lambda item: {
            "title": item.title,
            "path": item.path,
            "status": item.status,
            "front_matter": item.front_matter,
            "date": item.date,
            "filename": item.filename
        }
    )


def _bulk_import(model_class, data, field_mapping_func, model_name):
    """通用批量导入函数"""
    try:
        # 删除现有数据
        model_class.objects.all().delete()

        # 批量创建新对象
        objects = [field_mapping_func(item) for item in data]
        model_class.objects.bulk_create(objects)

        logging.info(gettext("IMPORT_SUCCESS").format(model_name))
        return True
    except Exception as e:
        logging.error(gettext("IMPORT_FAILED").format(model_name, str(e)))
        return False


def import_settings(ss):
    return _bulk_import(
        SettingModel,
        ss,
        lambda s: SettingModel(
            name=s["name"],
            content=s["content"]
        ),
        "设置"
    )


def import_images(ss):
    return _bulk_import(
        ImageModel,
        ss,
        lambda s: ImageModel(
            name=s["name"],
            url=s["url"],
            size=s["size"],
            date=s["date"],
            type=s["type"],
            deleteConfig=s["deleteConfig"]
        ),
        "图片"
    )


def import_friends(ss):
    return _bulk_import(
        FriendModel,
        ss,
        lambda s: FriendModel(
            name=s["name"],
            url=s["url"],
            imageUrl=s["imageUrl"],
            time=s["time"],
            description=s["description"],
            status=s["status"]
        ),
        "友链"
    )


def import_notifications(ss):
    return _bulk_import(
        NotificationModel,
        ss,
        lambda s: NotificationModel(
            time=s["time"],
            label=s["label"],
            content=s["content"]
        ),
        "通知"
    )


def import_custom(ss):
    return _bulk_import(
        CustomModel,
        ss,
        lambda s: CustomModel(
            name=s["name"],
            content=s["content"]
        ),
        "自定义"
    )


def import_uv(ss):
    return _bulk_import(
        StatisticUV,
        ss,
        lambda s: StatisticUV(ip=s["ip"]),
        "UV统计"
    )


def import_pv(ss):
    return _bulk_import(
        StatisticPV,
        ss,
        lambda s: StatisticPV(
            url=s["url"],
            number=s["number"]
        ),
        "PV统计"
    )


def import_talks(ss):
    return _bulk_import(
        TalkModel,
        ss,
        lambda s: TalkModel(
            content=s["content"],
            tags=s["tags"],
            time=s["time"],
            like=s["like"]
        ),
        "说说"
    )


def import_posts(ss):
    return _bulk_import(
        PostModel,
        ss,
        lambda s: PostModel(
            title=s["title"],
            path=s["path"],
            status=s["status"],
            front_matter=s["front_matter"],
            date=s["date"],
            filename=s["filename"]
        ),
        "文章"
    )


def excerpt_post(content, length, mark=True):
    if content is None:
        content = ""
    result, content = "", (markdown(content) if mark else content)
    soup = BeautifulSoup(content, 'html.parser')
    for dom in soup:
        if dom.name and dom.name not in ["script", "style"]:
            result += re.sub("{(.*?)}", '', dom.get_text()).replace("\n", " ")
            result += "" if result.endswith(" ") else " "
    return result[:int(length)] + "..." if (len(result) if result else 0) > int(length) else result


def edit_talk(_id, content):
    talk = TalkModel.objects.get(id=_id)
    talk.content = content
    talk.save()
    return True


def escapeString(_str):
    if not _str:
        return ""
    return escape(_str)


def mark_post(path, front_matter, status, filename):
    p = PostModel.objects.filter(path=path)
    if p:
        p.first().delete()
        PostModel.objects.create(
            title=front_matter.get("title") if front_matter.get("title") else gettext("UNTITLED"),
            path=path,
            status=status,
            front_matter=json.dumps(front_matter),
            date=time(),
            filename=filename
        )
        logging.info(f"{gettext('UPDATE_POST_INDEX')}：{path}")
    else:
        PostModel.objects.create(
            title=front_matter.get("title") if front_matter.get("title") else gettext("UNTITLED"),
            path=path,
            status=status,
            front_matter=json.dumps(front_matter),
            date=time(),
            filename=filename
        )
        logging.info(f"{gettext('UPDATE_POST_INDEX')}：{path}")


def del_postmark(path):
    p = PostModel.objects.filter(path=path)
    if p:
        p.first().delete()
        logging.info(f"{gettext('DEL_POST_INDEX')}：{path}")


def del_all_postmark():
    PostModel.objects.all().delete()


def convert_to_kb_mb_gb(size_in_bytes):
    kb = size_in_bytes / 1024
    mb = kb / 1024
    gb = mb / 1024
    if gb >= 1:
        return f"{gb:.2f} GB"
    elif mb >= 1:
        return f"{mb:.2f} MB"
    elif kb >= 1:
        return f"{kb:.2f} KB"
    else:
        return f"{size_in_bytes} B"


def get_domain_and_path(url):
    parsed = urlparse(url if "://" in url else "//" + url)
    domain = parsed.netloc
    path = domain + parsed.path if domain else parsed.path
    return domain, path

def get_db_config():
    return DATABASES["default"]["ENGINE"]


# print(" ......................阿弥陀佛......................\n" +
#       "                       _oo0oo_                      \n" +
#       "                      o8888888o                     \n" +
#       "                      88\" . \"88                     \n" +
#       "                      (| -_- |)                     \n" +
#       "                      0\\  =  /0                     \n" +
#       "                   ___/‘---’\\___                   \n" +
#       "                  .' \\|       |/ '.                 \n" +
#       "                 / \\\\|||  :  |||// \\                \n" +
#       "                / _||||| -卍-|||||_ \\               \n" +
#       "               |   | \\\\\\  -  /// |   |              \n" +
#       "               | \\_|  ''\\---/''  |_/ |              \n" +
#       "               \\  .-\\__  '-'  ___/-. /              \n" +
#       "             ___'. .'  /--.--\\  '. .'___            \n" +
#       "         .\"\" ‘<  ‘.___\\_<|>_/___.’>’ \"\".          \n" +
#       "       | | :  ‘- \\‘.;‘\\ _ /’;.’/ - ’ : | |        \n" +
#       "         \\  \\ ‘_.   \\_ __\\ /__ _/   .-’ /  /        \n" +
#       "    =====‘-.____‘.___ \\_____/___.-’___.-’=====     \n" +
#       "                       ‘=---=’                      \n" +
#       "                                                    \n" +
#       "....................佛祖保佑 ,永无BUG...................")

import platform as pf

print("           _               _ \n" +
      "     /\\   | |             | |\n" +
      "    /  \\  | |__  _   _  __| |_   _ \n" +
      "   / /\\ \\ | |_ \\| | | |/ _| | | | |\n" +
      "  / ____ \\| |_) | |_| | (_| | |_| |\n" +
      " /_/    \\_\\____/ \\____|\\____|\\____|")
print(gettext("CURRENT_ENV") + ": " + ("Vercel" if check_if_vercel() else gettext("LOCAL")) + " / " + (
    "Docker" if check_if_docker() else pf.system()) + " / Qexo " + QEXO_VERSION + " / Python " + pf.python_version() + " / " + get_db_config())

if check_if_vercel():
    logging.info = logging.warn

UPDATE_FROM = get_setting("UPDATE_FROM")
if UPDATE_FROM != "false" and UPDATE_FROM != "true" and UPDATE_FROM != QEXO_VERSION and UPDATE_FROM:
    logging.info(gettext("ELEVATOR_START").format(UPDATE_FROM))
    try:
        elevator.elevator(UPDATE_FROM, QEXO_VERSION)
    except Exception as e:
        logging.error(gettext("ELEVATOR_ERROR").format(str(e)))
    save_setting("UPDATE_FROM", QEXO_VERSION)
    save_setting("JUMP_UPDATE", "true")
