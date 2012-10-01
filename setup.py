from setuptools import setup, find_packages
 
setup(name='django-sqlfixtables',
    version="1.2",
    description='Management command for fixing tables after model changes',
    author='Dennis Kaarsemaker',
    author_email='dennis@kaarsemaker.net',
    url='http://github.com/seveas/django-sqlfixtables',
    packages=find_packages(),
    classifiers=[
        "Framework :: Django",
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Topic :: Software Development"
    ],
)
