import datetime as dt
import re
import uuid

import pytz
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone as djangotime
from loguru import logger
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from agents.models import Agent
from core.models import CoreSettings
from tacticalrmm.utils import notify_error

from .models import Client, ClientCustomField, Deployment, Site, SiteCustomField
from .serializers import (
    ClientCustomFieldSerializer,
    ClientSerializer,
    ClientTreeSerializer,
    DeploymentSerializer,
    SiteCustomFieldSerializer,
    SiteSerializer,
)

logger.configure(**settings.LOG_CONFIG)


class GetAddClients(APIView):
    def get(self, request):
        clients = Client.objects.all()
        return Response(ClientSerializer(clients, many=True).data)

    def post(self, request):
        # create client
        client_serializer = ClientSerializer(data=request.data["client"])
        client_serializer.is_valid(raise_exception=True)
        client = client_serializer.save()

        # create site
        site_serializer = SiteSerializer(
            data={"client": client.id, "name": request.data["site"]["name"]}
        )

        # make sure site serializer doesn't return errors and save
        if site_serializer.is_valid():
            site_serializer.save()
        else:
            # delete client since site serializer was invalid
            client.delete()
            site_serializer.is_valid(raise_exception=True)

        if "initialsetup" in request.data.keys():
            core = CoreSettings.objects.first()
            core.default_time_zone = request.data["timezone"]
            core.save(update_fields=["default_time_zone"])

        # save custom fields
        if "custom_fields" in request.data.keys():
            for field in request.data["custom_fields"]:

                custom_field = field
                custom_field["client"] = client.id

                serializer = ClientCustomFieldSerializer(data=custom_field)
                serializer.is_valid(raise_exception=True)
                serializer.save()

        return Response(f"{client} was added!")


class GetUpdateClient(APIView):
    def get(self, request, pk):
        client = get_object_or_404(Client, pk=pk)
        return Response(ClientSerializer(client).data)

    def put(self, request, pk):
        client = get_object_or_404(Client, pk=pk)

        serializer = ClientSerializer(
            data=request.data["client"], instance=client, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # update custom fields
        if "custom_fields" in request.data.keys():
            for field in request.data["custom_fields"]:

                custom_field = field
                custom_field["client"] = pk

                if ClientCustomField.objects.filter(field=field["field"], client=pk):
                    value = ClientCustomField.objects.get(
                        field=field["field"], client=pk
                    )
                    serializer = ClientCustomFieldSerializer(
                        instance=value, data=custom_field
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                else:
                    serializer = ClientCustomFieldSerializer(data=custom_field)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

        return Response("The Client was updated")


class DeleteClient(APIView):
    def delete(self, request, pk, sitepk):
        from automation.tasks import generate_all_agent_checks_task

        client = get_object_or_404(Client, pk=pk)
        agents = Agent.objects.filter(site__client=client)

        if not sitepk:
            return notify_error(
                "There needs to be a site specified to move existing agents to"
            )

        site = get_object_or_404(Site, pk=sitepk)
        agents.update(site=site)

        generate_all_agent_checks_task.delay("workstation", create_tasks=True)
        generate_all_agent_checks_task.delay("server", create_tasks=True)

        client.delete()
        return Response(f"{client.name} was deleted!")


class GetClientTree(APIView):
    def get(self, request):
        clients = Client.objects.all()
        return Response(ClientTreeSerializer(clients, many=True).data)


class GetAddSites(APIView):
    def get(self, request):
        sites = Site.objects.all()
        return Response(SiteSerializer(sites, many=True).data)

    def post(self, request):
        serializer = SiteSerializer(data=request.data["site"])
        serializer.is_valid(raise_exception=True)
        site = serializer.save()

        # save custom fields
        if "custom_fields" in request.data.keys():

            for field in request.data["custom_fields"]:

                custom_field = field
                custom_field["site"] = site.id

                serializer = SiteCustomFieldSerializer(data=custom_field)
                serializer.is_valid(raise_exception=True)
                serializer.save()

        return Response(f"Site {site.name} was added!")


class GetUpdateSite(APIView):
    def get(self, request, pk):
        site = get_object_or_404(Site, pk=pk)
        return Response(SiteSerializer(site).data)

    def put(self, request, pk):
        site = get_object_or_404(Site, pk=pk)

        if "client" in request.data["site"].keys() and (
            site.client.id != request.data["site"]["client"]
            and site.client.sites.count() == 1
        ):
            return notify_error("A client must have at least one site")

        serializer = SiteSerializer(
            instance=site, data=request.data["site"], partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # update custom field
        if "custom_fields" in request.data.keys():

            for field in request.data["custom_fields"]:

                custom_field = field
                custom_field["site"] = pk

                if SiteCustomField.objects.filter(field=field["field"], site=pk):
                    value = SiteCustomField.objects.get(field=field["field"], site=pk)
                    serializer = SiteCustomFieldSerializer(
                        instance=value, data=custom_field, partial=True
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                else:
                    serializer = SiteCustomFieldSerializer(data=custom_field)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

        return Response("Site was edited!")


class DeleteSite(APIView):
    def delete(self, request, pk, sitepk):
        from automation.tasks import generate_all_agent_checks_task

        site = get_object_or_404(Site, pk=pk)
        if site.client.sites.count() == 1:
            return notify_error("A client must have at least 1 site.")

        agents = Agent.objects.filter(site=site)

        if not sitepk:
            return notify_error(
                "There needs to be a site specified to move the agents to"
            )

        agent_site = get_object_or_404(Site, pk=sitepk)

        agents.update(site=agent_site)

        generate_all_agent_checks_task.delay("workstation", create_tasks=True)
        generate_all_agent_checks_task.delay("server", create_tasks=True)

        site.delete()
        return Response(f"{site.name} was deleted!")


class AgentDeployment(APIView):
    def get(self, request):
        deps = Deployment.objects.all()
        return Response(DeploymentSerializer(deps, many=True).data)

    def post(self, request):
        from knox.models import AuthToken

        client = get_object_or_404(Client, pk=request.data["client"])
        site = get_object_or_404(Site, pk=request.data["site"])

        expires = dt.datetime.strptime(
            request.data["expires"], "%Y-%m-%d %H:%M"
        ).astimezone(pytz.timezone("UTC"))
        now = djangotime.now()
        delta = expires - now
        obj, token = AuthToken.objects.create(user=request.user, expiry=delta)

        flags = {
            "power": request.data["power"],
            "ping": request.data["ping"],
            "rdp": request.data["rdp"],
        }

        Deployment(
            client=client,
            site=site,
            expiry=expires,
            mon_type=request.data["agenttype"],
            arch=request.data["arch"],
            auth_token=obj,
            token_key=token,
            install_flags=flags,
        ).save()
        return Response("ok")

    def delete(self, request, pk):
        d = get_object_or_404(Deployment, pk=pk)
        try:
            d.auth_token.delete()
        except:
            pass

        d.delete()
        return Response("ok")


class GenerateAgent(APIView):

    permission_classes = (AllowAny,)

    def get(self, request, uid):
        import tempfile
        import requests
        from django.http import FileResponse

        try:
            _ = uuid.UUID(uid, version=4)
        except ValueError:
            return notify_error("invalid")

        d = get_object_or_404(Deployment, uid=uid)

        inno = (
            f"winagent-v{settings.LATEST_AGENT_VER}.exe"
            if d.arch == "64"
            else f"winagent-v{settings.LATEST_AGENT_VER}-x86.exe"
        )
        client = d.client.name.replace(" ", "").lower()
        site = d.site.name.replace(" ", "").lower()
        client = re.sub(r"([^a-zA-Z0-9]+)", "", client)
        site = re.sub(r"([^a-zA-Z0-9]+)", "", site)
        ext = ".exe" if d.arch == "64" else "-x86.exe"
        file_name = f"rmm-{client}-{site}-{d.mon_type}{ext}"

        data = {
            "client": d.client.pk,
            "site": d.site.pk,
            "agenttype": d.mon_type,
            "rdp": str(d.install_flags["rdp"]),
            "ping": str(d.install_flags["ping"]),
            "power": str(d.install_flags["power"]),
            "goarch": "amd64" if d.arch == "64" else "386",
            "token": d.token_key,
            "inno": inno,
            "url": settings.DL_64 if d.arch == "64" else settings.DL_32,
            "api": f"https://{request.get_host()}",
        }
        headers = {"Content-type": "application/json"}

        with tempfile.NamedTemporaryFile() as fp:
            try:
                r = requests.post(
                    settings.EXE_GEN_URL,
                    json=data,
                    headers=headers,
                    stream=True,
                    timeout=900,
                )
            except Exception as e:
                logger.error(str(e))
                return notify_error(
                    "Something went wrong. Check debug error log for exact error message"
                )

            with open(fp.name, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
            del r
            response = FileResponse(
                open(fp.name, "rb"), as_attachment=True, filename=file_name
            )
            return response
