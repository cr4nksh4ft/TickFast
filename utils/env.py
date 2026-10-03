import os
import re

import dotenv


class Env:

    @staticmethod
    def _get_env_path():

        path = os.path.abspath(os.path.join(os.path.dirname(__file__),'..','.env'))

        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            raise Exception(f"Environment variable file at path {path} doesn't exist")
        return path

    def load(self):

        dotenv.load_dotenv(self._get_env_path(),override=True)

        return self

    def _get_all_names():

        lines = []
        with open(Env._get_env_path(),'r') as file:
            for line in file:
                lines.append(line.strip('\n\r'))

        names = []

        for line in lines:
            name = re.search(r"^\w+",line)

            if name:
                names.append(name.group(0))
        return names

    def validate(self):

        names = self._get_all_names()

        for name in names:

            value = self.get(name)

            if not value or value == "null":
                raise Exception("{value} value is not set!")

    @staticmethod
    def get(name, default=''):
        env = os.getenv(name,None)

        if not env:
            return default

        return env

def env(name, default=''):

    if not Env().get('APP_ENV'):
        Env().load().validate()

    return Env().get(name,default)
